"""Subagent service — manages async subagent lifecycle with Claude Code CLI backend."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from cyborg_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

# Module-level tracking for running async tasks and per-subagent locks
_running_tasks: dict[str, asyncio.Task[None]] = {}
_locks: dict[str, asyncio.Lock] = {}

SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent of Cyborg, an AI assistant. You have been given a task to complete.
Use your available tools (Read, Write, Glob, Grep) to accomplish the task.
Your working directory is the workspace — write files here by default (no absolute paths needed).
Provide clear, concise output describing what you did and what the result is.
"""

LOCAL_SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent of Cyborg. You have been assigned a task.
Use your available tools to accomplish it.
Your working directory is the workspace root.
Provide clear, concise output describing what you did and what the result is.
When done, output your final answer as plain text.
"""


def _get_lock(subagent_id: str) -> asyncio.Lock:
    if subagent_id not in _locks:
        _locks[subagent_id] = asyncio.Lock()
    return _locks[subagent_id]


class SubagentService(BaseService):
    """Manages async subagent lifecycle — create, run, message, check, list, kill."""

    async def create_subagent(
        self, task: str, parent_session_key: str, *, agent_type: str = "claude", persona: bool = False, model: str = "",
    ) -> dict[str, Any]:
        subagent_id = str(uuid4())
        short_id = subagent_id[:8]
        session_key = f"subagent:{parent_session_key}:{short_id}"
        now = utcnow().isoformat()

        await self.db.execute(
            """INSERT INTO subagents
               (id, parent_session_key, session_key, task, status, agent_type, persona, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?)""",
            (subagent_id, parent_session_key, session_key, task, agent_type, int(persona), model, now, now),
        )

        t = asyncio.create_task(self._run_subagent(subagent_id, task))
        _running_tasks[subagent_id] = t

        logger.info("Subagent created: id=%s session=%s", short_id, session_key)
        return {
            "ok": True,
            "subagent_id": subagent_id,
            "session_key": session_key,
            "status": "created",
        }

    async def _run_subagent(self, subagent_id: str, task: str) -> None:
        short_id = subagent_id[:8]
        await self._update_status(subagent_id, "running")

        row = await self.db.fetch_one(
            "SELECT agent_type, persona, session_key, model FROM subagents WHERE id = ?",
            (subagent_id,),
        )
        agent_type = row["agent_type"] if row else "claude"
        session_key = row["session_key"] if row else ""
        persona = bool(row["persona"]) if row else False
        model = row["model"] if row else ""

        settings = self._get_settings()

        try:
            if agent_type == "local":
                # Store user message in session before execution
                from cyborg_server.services.session_service import SessionService
                await SessionService(self.ctx).add_message(
                    session_key, "user", task, channel="subagent",
                )
                result = await self._run_local(
                    session_key=session_key,
                    persona=persona,
                    model=model,
                )
            else:
                workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
                result = await self._run_claude(
                    prompt=task,
                    cwd=workspace_dir,
                    model=settings.harness.skill_dev_model,
                    max_budget=settings.harness.skill_dev_max_budget_usd,
                )
        except Exception as e:
            logger.error("Subagent %s failed: %s", short_id, e)
            await self._update_status(subagent_id, "failed", error=str(e))
            await self._notify_parent(subagent_id, f"ERROR: {e}")
            _running_tasks.pop(subagent_id, None)
            return

        claude_session_id = result.get("session_id", "")
        result_text = result.get("result", "")
        cost = result.get("cost_usd", 0)

        now = utcnow().isoformat()
        await self.db.execute(
            """UPDATE subagents
               SET status = 'waiting_for_parent', result = ?,
                   claude_session_id = ?, cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (result_text, claude_session_id, cost, now, subagent_id),
        )

        # Store assistant message in subagent session
        # (user message already stored before execution for local, or stored here for claude)
        from cyborg_server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        if agent_type != "local":
            await session_svc.add_message(session_key, "user", task, channel="subagent")
        await session_svc.add_message(session_key, "assistant", result_text, channel="subagent")

        await self._publish_event(subagent_id, "result_ready")
        await self._notify_parent(subagent_id, result_text)

        logger.info(
            "Subagent %s waiting for parent: cost=%.4f chars=%d",
            short_id, cost, len(result_text),
        )
        _running_tasks.pop(subagent_id, None)

    async def message_subagent(self, subagent_id: str, message: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT * FROM subagents WHERE id = ?", (subagent_id,),
        )
        if row is None:
            return {"ok": False, "error": "Subagent not found"}
        if row["status"] not in ("waiting_for_parent", "running"):
            return {"ok": False, "error": f"Subagent is in status '{row['status']}', cannot receive messages"}

        agent_type = row["agent_type"]
        persona = bool(row["persona"])
        session_key = row["session_key"]
        model = row["model"]

        async with _get_lock(subagent_id):
            await self._update_status(subagent_id, "running")

            settings = self._get_settings()

            # Store user message before execution (local needs it in session history)
            from cyborg_server.services.session_service import SessionService
            session_svc = SessionService(self.ctx)
            if agent_type == "local":
                await session_svc.add_message(session_key, "user", message, channel="subagent")

            try:
                if agent_type == "local":
                    result = await self._run_local(
                        session_key=session_key,
                        persona=persona,
                        model=model,
                    )
                else:
                    workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
                    result = await self._run_claude(
                        prompt=message,
                        cwd=workspace_dir,
                        session_id=row["claude_session_id"],
                        model=settings.harness.skill_dev_model,
                        max_budget=settings.harness.skill_dev_max_budget_usd,
                    )
            except Exception as e:
                await self._update_status(subagent_id, "failed", error=str(e))
                return {"ok": False, "error": str(e), "subagent_id": subagent_id}

            result_text = result.get("result", "")
            claude_session_id = result.get("session_id", row["claude_session_id"])
            cost = result.get("cost_usd", 0)
            total_cost = (row["cost_usd"] or 0) + cost

            now = utcnow().isoformat()
            await self.db.execute(
                """UPDATE subagents
                   SET status = 'waiting_for_parent', result = ?,
                       claude_session_id = ?, cost_usd = ?, updated_at = ?
                   WHERE id = ?""",
                (result_text, claude_session_id, total_cost, now, subagent_id),
            )

            # Store messages in subagent session
            if agent_type != "local":
                await session_svc.add_message(session_key, "user", message, channel="subagent")
            await session_svc.add_message(session_key, "assistant", result_text, channel="subagent")

            await self._publish_event(subagent_id, "result_ready")
            await self._notify_parent(subagent_id, result_text)

            logger.info("Subagent %s messaged: cost=%.4f", subagent_id[:8], total_cost)
            return {"ok": True, "result": result_text, "subagent_id": subagent_id}

    async def check_subagent(self, subagent_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT id, status, result, error_message, cost_usd, task, created_at FROM subagents WHERE id = ?",
            (subagent_id,),
        )
        if row is None:
            return {"ok": False, "error": "Subagent not found"}
        return {
            "ok": True,
            "subagent_id": row["id"],
            "status": row["status"],
            "result": row["result"],
            "error": row["error_message"],
            "cost_usd": row["cost_usd"],
            "task_preview": (row["task"] or "")[:100],
            "created_at": row["created_at"],
        }

    async def list_subagents(self, parent_session_key: str, status: str = "") -> list[dict[str, Any]]:
        query = (
            "SELECT id, status, substr(task, 1, 100) as task_preview, cost_usd, created_at "
            "FROM subagents WHERE parent_session_key = ?"
        )
        params: list[str] = [parent_session_key]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT 20"
        rows = await self.db.fetch_all(query, tuple(params))
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "task": row["task_preview"],
                "cost_usd": row["cost_usd"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def kill_subagent(self, subagent_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT status FROM subagents WHERE id = ?", (subagent_id,),
        )
        if row is None:
            return {"ok": False, "error": "Subagent not found"}

        task = _running_tasks.pop(subagent_id, None)
        if task and not task.done():
            task.cancel()

        await self._update_status(subagent_id, "killed")
        logger.info("Subagent %s killed", subagent_id[:8])
        return {"ok": True, "subagent_id": subagent_id, "status": "killed"}

    async def cleanup_stale(self) -> int:
        """Set any running subagents to failed (e.g. after server restart)."""
        now = utcnow().isoformat()
        count = await self.db.execute(
            "UPDATE subagents SET status = 'failed', error_message = 'Server restarted', updated_at = ? "
            "WHERE status IN ('created', 'running')",
            (now,),
        )
        if count:
            logger.info("Cleaned up %d stale subagents", count)
        return count

    # -- Internal helpers --

    async def _notify_parent(self, subagent_id: str, result_text: str) -> None:
        """Inject a subagent result message into the parent session and publish event."""
        row = await self.db.fetch_one(
            "SELECT parent_session_key FROM subagents WHERE id = ?",
            (subagent_id,),
        )
        if not row:
            return

        short_id = subagent_id[:8]
        content = (
            f"[Subagent {short_id}] {result_text}\n\n"
            f"Relay this result to the user by calling send_whatsapp_message with a summary. "
            f"You can also use message_subagent to reply or kill_subagent to terminate."
        )

        from cyborg_server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        await session_svc.add_message(
            row["parent_session_key"],
            "user",
            content,
            channel="subagent",
            dispatched=0,
        )

        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("subagent.result_ready", {
                "subagent_id": subagent_id,
                "parent_session_key": row["parent_session_key"],
                "result": result_text,
            })

    async def _update_status(
        self,
        subagent_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        if error:
            await self.db.execute(
                "UPDATE subagents SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, error, now, subagent_id),
            )
        else:
            await self.db.execute(
                "UPDATE subagents SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, subagent_id),
            )
        await self._publish_event(subagent_id, status)

    async def _publish_event(self, subagent_id: str, status: str) -> None:
        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("subagent.updated", {
                "subagent_id": subagent_id,
                "status": status,
            })

    async def _run_local(
        self,
        *,
        session_key: str,
        persona: bool = False,
        model: str = "",
    ) -> dict[str, Any]:
        """Run a subagent in-process using the existing chat_with_tools loop."""
        settings = self._get_settings()
        resolved_model = model or settings.harness.local_subagent_model

        # Build system prompt
        if persona:
            from cyborg_server.services.prompt_assembler import load_workspace_prompt
            system_content = await load_workspace_prompt(
                settings.harness.workspace_dir, db=self.db,
            )
        else:
            workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
            system_content = (
                LOCAL_SUBAGENT_SYSTEM_PROMPT
                + f"\nYour workspace root is: {workspace_dir}"
            )

        # Build workspace-only tool set
        from cyborg_server.services.workspace_tools import make_workspace_tools
        tools = make_workspace_tools(self.ctx, session_key=session_key)

        # Build messages from session history
        from cyborg_server.services.prompt_assembler import build_chat_messages
        messages = await build_chat_messages(
            None, session_key,
            db=self.db,
            system_content=system_content,
            max_history=50,
        )

        # Dispatch via LLM dispatch (logs calls, publishes events)
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        result_text = await LLMDispatchService(self.ctx).chat_with_tools(
            messages=messages,
            tools=tools,
            model=resolved_model,
            max_iterations=30,
            call_category="local_subagent",
            session_key=session_key,
        )

        logger.info(
            "Local subagent: model=%s chars=%d",
            resolved_model, len(result_text),
        )

        return {"result": result_text, "session_id": "", "cost_usd": 0}

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
            "--allowed-tools", "Read Write Glob Grep Bash",
            "--system-prompt", SUBAGENT_SYSTEM_PROMPT,
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
            return {"result": output, "session_id": "", "cost_usd": 0}
