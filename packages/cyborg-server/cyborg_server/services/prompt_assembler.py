"""Assembles prompt messages from workspace files and session history."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORKSPACE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")

# Module-level cache for workspace file content.
_cached_prompt: tuple[Any, str] | None = None  # (mtime_hash, content)
_cached_mtime: dict[str, float] = {}


async def load_workspace_prompt(workspace_dir: Path, db: Any = None) -> str:
    """Load and concatenate workspace files. Cached until any file changes."""
    global _cached_prompt, _cached_mtime

    workspace_dir = workspace_dir.expanduser()
    mtimes: dict[str, float] = {}
    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        mtimes[name] = path.stat().st_mtime if path.is_file() else 0.0

    # Include skill file mtimes so new/changed skills invalidate the cache
    skills_dir = workspace_dir / "skills"
    if skills_dir.is_dir():
        for skill_path in sorted(skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue
            md = skill_path / "skill.md"
            if not md.is_file():
                md = skill_path / "SKILL.md"
            if md.is_file():
                mtimes[f"skills/{skill_path.name}"] = md.stat().st_mtime

    mtime_hash = tuple(mtimes.items())
    if _cached_prompt is not None and _cached_prompt[0] == mtime_hash:
        return _cached_prompt[1]

    parts: list[str] = []
    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

    # Load skills index (lightweight — full skill loaded on-demand via use_skill tool)
    from cyborg_server.services.skill_loader import load_skills_index
    skills_index = load_skills_index(workspace_dir)
    if skills_index:
        parts.append("## Available Skills\n\n" + skills_index)

    # Load memory index from SQLite
    if db is not None:
        from cyborg_server.services.memory.service import build_memory_index_text_db
        # TODO: The full memory index dump was ~22KB+ and mostly artifact noise.
        # The memory tools (search/read/browse/write/graph) still work — agents
        # discover entities on demand rather than via a prompt dump that probably
        # didn't help anyway. Re-enable if we build a compact, useful index.
        # memory_index = await build_memory_index_text_db(db)
        memory_section = (
            "## Memory\n\n"
            "You have persistent memory with these tools:\n"
            "- **memory_search(query, entity_type?)** — Always start here. Searches all entities and returns an abstract with matches.\n"
            "- **memory_read(entity_id)** — Read a specific entity in full (e.g. contact-7c9f0fd7).\n"
            "- **memory_browse(entity_type)** — List all entities of a type.\n"
            "- **memory_write(content, channel_id?, visibility?)** — Write a new bulletin (queued, curated by dream process).\n"
            "- **memory_graph(entity_id, depth?)** — Explore related entities.\n"
            "- **memory_correct(action, entity_id?, claim_type_key?, value?, reason)** — Correct wrong memory. Actions: remove_entity (archive entity + all claims), remove_claim (supersede a specific claim), set_truth (write a user correction). Always provide a reason.\n"
            "\n"
            "Entity types: contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions.\n"
        )
        # if memory_index:
        #     memory_section += "\n" + memory_index
        parts.append(memory_section)

    # Append grounding rules to reduce hallucinated tool claims
    parts.append(
        "## CRITICAL: How to Respond\n"
        "Your text output is NOT delivered to the user. Only tool calls have effect.\n"
        "ALWAYS call send_whatsapp_message (or email_reply) as your final action — even for short replies, "
        "even for acknowledgments, even for jokes. Without that call, nothing is sent.\n"
        "Use as many tools as you need before replying — memory, files, docs, contacts, scripts.\n"
    )
    parts.append(
        "## Grounding Rules\n"
        "- Only state that you have done something if you used a tool that confirmed success.\n"
        "- If you did not call a tool, the action did not happen — do not claim it did.\n"
        "- If a tool returns an error, report the error honestly — do not pretend it succeeded.\n"
        "- If you are unsure whether you can do something, say so. Do not claim capabilities you have not verified.\n"
    )

    workspace_resolved = workspace_dir.expanduser().resolve()
    parts.append(
        "## Workspace\n"
        f"Your workspace root is: {workspace_resolved}\n"
        "File tool paths can be absolute (within this directory) or relative to workspace root.\n"
        "All file operations are restricted to this directory."
    )

    combined = "\n\n".join(parts)
    _cached_prompt = (mtime_hash, combined)
    if _cached_mtime != mtimes:
        logger.info(
            "Workspace loaded: dir=%s chars=%d files=%s",
            workspace_dir, len(combined),
            [n for n in _WORKSPACE_FILES if mtimes.get(n)],
        )
        _cached_mtime.update(mtimes)
    return combined


def _resolve_mentions(text: str, mention_names: dict[str, str]) -> str:
    """Replace @digits patterns with display names from the mention map."""
    def _replace(m: re.Match[str]) -> str:
        digits = m.group(1)
        name = mention_names.get(digits)
        return f"@{name}" if name else m.group(0)
    return re.sub(r"@(\d{7,15})", _replace, text)


async def build_chat_messages(
    user_message: str | list[dict[str, Any]] | None = None,
    session_key: str = "",
    *,
    db: Any = None,
    system_content: str = "",
    voice_instructions: str = "",
    max_history: int = 20,
) -> list[dict[str, Any]]:
    """Build a messages array: system prompt + session history + optional user message."""
    system_parts: list[str] = []
    if system_content:
        system_parts.append(system_content)
    if voice_instructions:
        system_parts.append(voice_instructions)

    messages: list[dict[str, Any]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    if session_key and db is not None:
        is_group = ":group:" in session_key

        # For group sessions, resolve sender_id to display names
        sender_names: dict[str, str] = {}
        mention_names: dict[str, str] = {}
        if is_group:
            participants = await db.fetch_all(
                "SELECT contact_id, display_name, identifier FROM session_participants "
                "WHERE session_key = ?",
                (session_key,),
            )
            for p in participants:
                if p["contact_id"] and p["display_name"]:
                    sender_names[p["contact_id"]] = p["display_name"]
                if p["display_name"] and p["identifier"]:
                    digits = re.sub(r"\D", "", p["identifier"])
                    if digits:
                        mention_names[digits] = p["display_name"]

        rows = await db.fetch_all(
            "SELECT role, content, sender_id, metadata FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "AND rowid IN (SELECT rowid FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at DESC LIMIT ?) "
            "ORDER BY created_at ASC",
            (session_key, session_key, max_history),
        )
        for row in rows:
            if not row["content"]:
                continue
            # Skip stale NO_REPLY entries that poison future decisions
            if row["role"] == "assistant" and row["content"].strip().upper().rstrip(".") in (
                "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
            ):
                continue
            content = row["content"]
            if is_group and mention_names:
                content = _resolve_mentions(content, mention_names)

            # Check for image metadata and reconstruct multimodal content
            meta: dict[str, Any] = {}
            raw_meta = row.get("metadata")
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                except (json.JSONDecodeError, TypeError):
                    pass
            image_path = meta.get("image_path")
            mime_type = meta.get("image_mime_type", "image/jpeg")

            if image_path and row["role"] == "user" and os.path.isfile(image_path):
                text_prefix = ""
                if is_group and row["sender_id"]:
                    name = sender_names.get(row["sender_id"])
                    if name:
                        text_prefix = f"[{name}] "
                with open(image_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode()
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text_prefix + content},
                        {"type": "input_image", "image_url": f"data:{mime_type};base64,{image_data}"},
                    ],
                })
                continue

            if is_group and row["role"] == "user" and row["sender_id"]:
                name = sender_names.get(row["sender_id"])
                if name:
                    messages.append({"role": "user", "content": f"[{name}] {content}"})
                    continue
            messages.append({"role": row["role"], "content": content})

    if user_message is not None:
        messages.append({"role": "user", "content": user_message})
    return messages
