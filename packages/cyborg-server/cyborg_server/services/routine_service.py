"""DB CRUD for routines."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from cyborg_server.services.base import BaseService


class RoutineService(BaseService):
    async def list_routines(self, session_key: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, session_key, name, schedule, prompt, enabled, next_run_at, last_run_at, created_at, updated_at "
            "FROM routines WHERE session_key = ? ORDER BY name",
            (session_key,),
        )
        return [dict(r) for r in rows] if rows else []

    async def get_routine(self, session_key: str, name: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT id, session_key, name, schedule, prompt, enabled, next_run_at, last_run_at, created_at, updated_at "
            "FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        return dict(row) if row else None

    async def upsert_routine(
        self,
        session_key: str,
        name: str,
        schedule: str,
        prompt: str,
        enabled: bool = True,
        next_run_at: str | None = None,
    ) -> dict[str, Any]:
        existing = await self.get_routine(session_key, name)
        now = datetime.now().astimezone().isoformat()

        if existing:
            await self.db.execute(
                "UPDATE routines SET schedule = ?, prompt = ?, enabled = ?, next_run_at = ?, updated_at = ? "
                "WHERE session_key = ? AND name = ?",
                (schedule, prompt, int(enabled), next_run_at, now, session_key, name),
            )
        else:
            await self.db.execute(
                "INSERT INTO routines (id, session_key, name, schedule, prompt, enabled, next_run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session_key, name, schedule, prompt, int(enabled), next_run_at, now, now),
            )

        result = await self.get_routine(session_key, name)
        assert result is not None
        return result

    async def delete_routine(self, session_key: str, name: str) -> bool:
        count = await self.db.execute(
            "DELETE FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        return count > 0

    async def get_due_routines(self) -> list[dict[str, Any]]:
        now = datetime.now().astimezone().isoformat()
        rows = await self.db.fetch_all(
            "SELECT id, session_key, name, schedule, prompt, enabled, next_run_at, last_run_at "
            "FROM routines WHERE enabled = 1 AND next_run_at <= ?",
            (now,),
        )
        return [dict(r) for r in rows] if rows else []

    async def mark_run(self, routine_id: str, next_run_at: str) -> None:
        now = datetime.now().astimezone().isoformat()
        await self.db.execute(
            "UPDATE routines SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (now, next_run_at, now, routine_id),
        )
