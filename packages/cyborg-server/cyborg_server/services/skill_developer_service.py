"""Orchestrate skill creation by delegating to Claude Code via subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

SKILL_DEV_SYSTEM_PROMPT = """\
You are a skill developer for an AI agent called Cyborg. \
Your job is to create executable Python skills that Cyborg can run. \
Every skill MUST include a Python helper script — skills are never markdown-only.

## Your Tools

You have these tools available:
- **Read**: Read file contents. Use this to examine existing skills and workspace files.
- **Write**: Create or overwrite files. Use this to create skill files.
- **Glob**: Find files matching a pattern. Use this to discover existing skills.
- **Grep**: Search file contents. Use this to search across workspace files.

## Skill Format

Each skill is a directory under `skills/` containing at minimum a `skill.md` and a \
`helper.py`. The helper.py is the executable core; skill.md tells Cyborg when and \
how to use it.

    skills/{skill_name}/
      skill.md          (required — trigger + instructions for Cyborg)
      helper.py         (required — the Python script Cyborg runs via run_script)
      pyproject.toml    (optional — if the script needs third-party dependencies)

### skill.md format

    ---
    name: skill_name
    description: One-line summary of what this skill does
    trigger: When or why this skill activates
    ---

    ## Instructions

    Step-by-step instructions for Cyborg to follow when this skill activates.
    Must include a step that calls `run_script` to execute helper.py.

    ## Example

    Concrete example of expected input/output.

### helper.py format

The script should:
- Accept file paths as command-line arguments (absolute paths provided by Cyborg)
- Use argparse or sys.argv for arguments
- Print results to stdout (Cyborg captures this as the tool result)
- Exit 0 on success, non-zero on failure (stderr is captured on error)
- Be self-contained — avoid assuming a specific working directory
- Do NOT resolve paths internally — use the paths passed as arguments directly

### pyproject.toml (optional)

If the script needs third-party packages (e.g. `requests`, `beautifulsoup4`), \
create a minimal pyproject.toml in the skill directory so `uv run` installs them:

    [project]
    name = "skill_name"
    version = "0.1.0"
    dependencies = ["requests"]

## Cyborg's Runtime Tools (Reference Only)

Skills can instruct Cyborg to use these tools. You cannot call them yourself:

- **run_script(path, args)**: Run a Python script in the workspace. \
  Use this to execute your helper.py.
- Communication: send_whatsapp_message, send_whatsapp_to_contact, email_reply, email_skip
- Workspace: ls, read_file, write_file, rm, mv, find, update_agenda
- Search: search_contacts

## Available Environment Variables

When helper.py runs via run_script, these standard environment variables are available:

- `OPENAI_API_KEY`: OpenAI API key (if configured). Use with `OpenAI()` directly.
- `OPENAI_BASE_URL`: Custom OpenAI API base URL (if configured).
- `AGENTMAIL_API_KEY`: AgentMail API key (if configured).
- `CYBORG_WORKSPACE_DIR`: Absolute path to the workspace root directory. \
  Useful as a fallback, but prefer receiving paths as command-line arguments.

Skills should use `os.environ.get("VAR_NAME")` or rely on SDK auto-detection. \
Do NOT reference CYBORG_-prefixed variable names OTHER THAN `CYBORG_WORKSPACE_DIR`.

## Workflow

When asked to create a skill:
1. Use Read or Glob to examine existing skills in `skills/` for patterns
2. Read workspace files (SOUL.md, IDENTITY.md, USER.md) for context about Cyborg's persona
3. Design the skill: name, trigger, what the Python script will do
4. Use Write to create ALL of these files:
   - `skills/{name}/skill.md` — trigger + instructions referencing run_script
   - `skills/{name}/helper.py` — the Python script with the actual logic
   - `skills/{name}/pyproject.toml` — only if third-party dependencies are needed
5. Test mentally: trace through what happens when Cyborg follows skill.md's \
   instructions and calls run_script on helper.py

Keep skills focused on a single capability. The helper.py must actually work — \
write real, runnable Python code, not pseudocode or stubs.
"""


class SkillDeveloperService(BaseService):
    """Manages skill creation delegations to Claude Code."""

    async def plan_skill(self, user_story: str, session_key: str) -> dict[str, Any]:
        """Send a user story to Claude Code for planning. Returns plan text."""
        delegation_id = str(uuid4())
        now = utcnow().isoformat()

        await self.db.execute(
            """INSERT INTO skill_delegations
               (id, session_key, user_story, status, created_at, updated_at)
               VALUES (?, ?, ?, 'planning', ?, ?)""",
            (delegation_id, session_key, user_story, now, now),
        )

        settings = self._get_settings()
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        skills_dir = workspace_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Gather context about existing workspace state
        existing_skills = sorted(
            d.name for d in skills_dir.iterdir()
            if d.is_dir() and (d / "skill.md").is_file()
        ) if skills_dir.is_dir() else []

        workspace_files = [
            f.name for f in workspace_dir.iterdir()
            if f.is_file() and f.suffix == ".md"
        ]

        context_parts = [f"USER STORY:\n{user_story}"]
        if existing_skills:
            context_parts.append(f"Existing skills: {', '.join(existing_skills)}")
        if workspace_files:
            context_parts.append(f"Workspace files: {', '.join(workspace_files)}")

        prompt = "\n".join(context_parts) + (
            "\n\nTASK: Plan a new skill for this request.\n\n"
            "Steps:\n"
            "1. Read 1-2 existing skills to understand the format and patterns\n"
            "2. Read relevant workspace files (SOUL.md, IDENTITY.md) for persona context\n"
            "3. Describe your plan: skill name, trigger, what the instructions will cover, "
            "whether any helper scripts are needed\n\n"
            "Do NOT create any files yet. Output your plan as clear text that can be "
            "relayed to the user for approval."
        )

        try:
            result = await self._run_claude(
                prompt,
                cwd=workspace_dir,
                model=settings.harness.skill_dev_model,
                max_budget=settings.harness.skill_dev_max_budget_usd,
            )
        except Exception as e:
            await self._update_status(delegation_id, "failed", error=str(e))
            return {"ok": False, "error": str(e), "delegation_id": delegation_id}

        claude_session_id = result.get("session_id", "")
        plan_text = result.get("result", "")
        cost = result.get("cost_usd", 0)

        await self.db.execute(
            """UPDATE skill_delegations
               SET plan = ?, claude_session_id = ?, status = 'plan_ready',
                   cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (plan_text, claude_session_id, cost, utcnow().isoformat(), delegation_id),
        )
        await self._publish_event(delegation_id, "plan_ready")

        logger.info(
            "Skill plan ready: delegation=%s session=%s cost=%.4f",
            delegation_id, claude_session_id, cost,
        )

        return {
            "ok": True,
            "delegation_id": delegation_id,
            "plan": plan_text,
            "cost_usd": cost,
        }

    async def implement_skill(self, delegation_id: str) -> dict[str, Any]:
        """Resume Claude Code session and implement the approved plan."""
        row = await self.db.fetch_one(
            "SELECT * FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}
        if row["status"] != "plan_ready":
            return {"ok": False, "error": f"Delegation is in status '{row['status']}', expected 'plan_ready'"}

        claude_session_id = row["claude_session_id"]
        if not claude_session_id:
            return {"ok": False, "error": "No Claude session ID to resume"}

        settings = self._get_settings()
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        skills_dir = workspace_dir / "skills"

        await self._update_status(delegation_id, "implementing")

        try:
            result = await self._run_claude(
                (
                    f"The user has approved your plan. Implement it now.\n\n"
                    f"APPROVED PLAN:\n{row['plan']}\n\n"
                    f"Create the skill files using the Write tool. "
                    f"Create files at paths like skills/{{name}}/skill.md "
                    f"(and any helper scripts alongside it). "
                    f"After creating files, briefly summarize what was created."
                ),
                cwd=workspace_dir,
                session_id=claude_session_id,
                model=settings.harness.skill_dev_model,
                max_budget=settings.harness.skill_dev_max_budget_usd,
            )
        except Exception as e:
            await self._update_status(delegation_id, "failed", error=str(e))
            return {"ok": False, "error": str(e), "delegation_id": delegation_id}

        result_text = result.get("result", "")
        cost = result.get("cost_usd", 0)
        total_cost = (row["cost_usd"] or 0) + cost

        # Discover what files were created
        files_created = await self._find_new_skills(skills_dir)

        await self.db.execute(
            """UPDATE skill_delegations
               SET status = 'completed', result_summary = ?,
                   files_created_json = ?, cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (
                result_text,
                json.dumps(files_created),
                total_cost,
                utcnow().isoformat(),
                delegation_id,
            ),
        )
        await self._publish_event(delegation_id, "completed")

        # Clear skill loader cache so new skills appear
        from cyborg_server.services.skill_loader import _skills_cache
        import cyborg_server.services.skill_loader as sl
        sl._skills_cache = None

        logger.info(
            "Skill implemented: delegation=%s files=%s cost=%.4f",
            delegation_id, files_created, total_cost,
        )

        return {
            "ok": True,
            "delegation_id": delegation_id,
            "result": result_text,
            "files_created": files_created,
            "cost_usd": total_cost,
        }

    async def reject_skill(self, delegation_id: str, reason: str) -> dict[str, Any]:
        """Reject a delegation plan."""
        row = await self.db.fetch_one(
            "SELECT status FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}

        await self._update_status(delegation_id, "rejected", error=reason)
        return {"ok": True, "delegation_id": delegation_id, "status": "rejected"}

    async def get_status(self, delegation_id: str) -> dict[str, Any]:
        """Get delegation status."""
        row = await self.db.fetch_one(
            "SELECT * FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}
        return {"ok": True, "delegation_id": delegation_id, "status": row["status"]}

    async def _run_claude(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        model: str = "sonnet",
        max_budget: float = 5.0,
    ) -> dict[str, Any]:
        """Run Claude Code as a subprocess and return JSON output."""
        claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        if not Path(claude_bin).is_file():
            raise RuntimeError(f"claude CLI not found (tried PATH and {claude_bin})")

        cmd = [
            claude_bin, "-p",
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(max_budget),
            "--allowed-tools", "Read Write Glob Grep",
            "--system-prompt", SKILL_DEV_SYSTEM_PROMPT,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)

        logger.info("Spawning Claude Code: session=%s cwd=%s", session_id, cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self._get_settings().harness.skill_dev_timeout_seconds,
        )

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.error("Claude Code failed (rc=%d): %s", proc.returncode, err)
            raise RuntimeError(f"Claude Code exited with code {proc.returncode}: {err[:500]}")

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            raise RuntimeError("Claude Code returned empty output")

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            # Fallback: treat raw text as result
            return {"result": output, "session_id": "", "cost_usd": 0}

    async def _update_status(
        self,
        delegation_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        if error:
            await self.db.execute(
                "UPDATE skill_delegations SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, error, now, delegation_id),
            )
        else:
            await self.db.execute(
                "UPDATE skill_delegations SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, delegation_id),
            )
        await self._publish_event(delegation_id, status)

    async def _publish_event(self, delegation_id: str, status: str) -> None:
        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("skill.delegation.updated", {
                "delegation_id": delegation_id,
                "status": status,
            })

    async def _find_new_skills(self, skills_dir: Path) -> list[str]:
        """List skill directories that contain a skill.md."""
        files = []
        if not skills_dir.is_dir():
            return files
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir() and (child / "skill.md").is_file():
                files.append(child.name)
        return files
