"""Discover and load skills from workspace/skills/ into the system prompt."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level caches keyed by mtime hash.
_skills_cache: tuple[Any, str] | None = None
_index_cache: tuple[Any, str] | None = None

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-like frontmatter key: value pairs from text."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _scan_skills(workspace_dir: Path) -> dict[str, float]:
    """Return {skill_name: mtime} for all skills with a skill.md or SKILL.md."""
    skills_dir = workspace_dir / "skills"
    if not skills_dir.is_dir():
        return {}
    mtimes: dict[str, float] = {}
    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue
        md = skill_path / "skill.md"
        if not md.is_file():
            md = skill_path / "SKILL.md"
        if md.is_file():
            mtimes[skill_path.name] = md.stat().st_mtime
    return mtimes


def load_skills_index(workspace_dir: Path) -> str:
    """Return a compact skill index (name, path, description, trigger) for the system prompt."""
    global _index_cache

    workspace_dir = workspace_dir.expanduser()
    mtimes = _scan_skills(workspace_dir)
    if not mtimes:
        return ""

    mtime_hash = tuple(sorted(mtimes.items()))
    if _index_cache is not None and _index_cache[0] == mtime_hash:
        return _index_cache[1]

    skills_dir = workspace_dir / "skills"
    lines: list[str] = []
    lines.append(
        "When a skill trigger matches the user's request, call the `use_skill` tool "
        "with the skill name to load the full instructions and script paths.\n"
    )
    for skill_name in sorted(mtimes):
        md = skills_dir / skill_name / "skill.md"
        if not md.is_file():
            md = skills_dir / skill_name / "SKILL.md"
        content = md.read_text(encoding="utf-8").strip()
        fm = _parse_frontmatter(content)
        desc = fm.get("description", "")
        trigger = fm.get("trigger", "")
        lines.append(f"- **{skill_name}** (skills/{skill_name}/): {desc}")
        if trigger:
            lines.append(f"  Trigger: {trigger}")

    combined = "\n".join(lines)
    _index_cache = (mtime_hash, combined)
    logger.info("Skills index: %d skills, %d chars", len(mtimes), len(combined))
    return combined


def load_skill(workspace_dir: Path, skill_name: str) -> str:
    """Load a single skill's full instructions, prefixed with its directory path."""
    workspace_dir = workspace_dir.expanduser()
    skill_dir = workspace_dir / "skills" / skill_name
    md = skill_dir / "skill.md"
    if not md.is_file():
        md = skill_dir / "SKILL.md"
    if not md.is_file():
        return f"Error: skill '{skill_name}' not found"
    content = md.read_text(encoding="utf-8").strip()
    return f"Skill: {skill_name}\nPath: {skill_dir.resolve()}/\n\n{content}"


def load_skills_prompt(workspace_dir: Path) -> str:
    """Load full skill.md content for all skills. Used for testing/diagnostics."""
    global _skills_cache

    workspace_dir = workspace_dir.expanduser()
    mtimes = _scan_skills(workspace_dir)
    if not mtimes:
        return ""

    mtime_hash = tuple(sorted(mtimes.items()))
    if _skills_cache is not None and _skills_cache[0] == mtime_hash:
        return _skills_cache[1]

    skills_dir = workspace_dir / "skills"
    parts: list[str] = []
    for skill_name in sorted(mtimes):
        md = skills_dir / skill_name / "skill.md"
        if not md.is_file():
            md = skills_dir / skill_name / "SKILL.md"
        content = md.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)

    if not parts:
        return ""

    combined = "\n\n---\n\n".join(parts)
    _skills_cache = (mtime_hash, combined)
    logger.info(
        "Skills loaded: dir=%s count=%d chars=%d",
        skills_dir, len(parts), len(combined),
    )
    return combined
