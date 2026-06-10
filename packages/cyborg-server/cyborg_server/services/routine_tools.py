"""Routine tools — read_routine, write_routine, delete_routine for LLM sessions."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import yaml

from cyborg_server.cron import next_cron_occurrence, validate_cron_expression
from cyborg_server.services.tools import Tool, tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def _routine_to_yaml(routine: dict) -> str:
    return yaml.dump(
        {
            "name": routine["name"],
            "schedule": routine["schedule"],
            "prompt": routine["prompt"],
            "enabled": bool(routine["enabled"]),
        },
        default_flow_style=False,
    )


def make_routine_tools(
    ctx: AppContext,
    *,
    session_key: str,
) -> list[Tool]:
    from cyborg_server.services.routine_service import RoutineService

    svc = RoutineService(ctx)

    @tool
    async def read_routine(name: str = "") -> str:
        """Read a routine by name, or list all routines for this session if name is omitted.
        Returns YAML for a single routine, or a JSON list of routine summaries."""
        if name.strip():
            routine = await svc.get_routine(session_key, name.strip())
            if not routine:
                return json.dumps({"error": f"Routine '{name}' not found"})
            return _routine_to_yaml(routine)

        routines = await svc.list_routines(session_key)
        if not routines:
            return "No routines configured for this session."

        summaries = [
            {"name": r["name"], "schedule": r["schedule"], "enabled": bool(r["enabled"])}
            for r in routines
        ]
        return json.dumps({"routines": summaries})

    @tool
    async def write_routine(routine_yaml: str) -> str:
        """Create or update a routine for this session. Accepts YAML with fields: name, schedule (cron), prompt, enabled.
        The prompt must contain ONLY the action to perform — never include schedule/timing language
        (e.g. "At 9am each day", "Every Monday"). The schedule is handled by the separate schedule field.
        Example:
          name: morning-digest
          schedule: "0 8 * * 1-5"
          prompt: Gather tech news and summarize.
          enabled: true"""
        try:
            parsed = yaml.safe_load(routine_yaml)
        except yaml.YAMLError as e:
            return json.dumps({"error": f"Invalid YAML: {e}"})

        if not isinstance(parsed, dict) or "name" not in parsed:
            return json.dumps({"error": "YAML must include a 'name' field"})

        name = parsed["name"]
        schedule = parsed.get("schedule", "")
        prompt = parsed.get("prompt", "")
        enabled = parsed.get("enabled", True)

        if not schedule:
            return json.dumps({"error": "Routine must include a 'schedule' field"})
        if not prompt:
            return json.dumps({"error": "Routine must include a 'prompt' field"})

        try:
            validate_cron_expression(schedule)
        except ValueError as e:
            return json.dumps({"error": f"Invalid cron expression: {e}"})

        next_at = next_cron_occurrence(schedule).isoformat()

        routine = await svc.upsert_routine(
            session_key=session_key,
            name=name,
            schedule=schedule,
            prompt=prompt,
            enabled=enabled,
            next_run_at=next_at,
        )
        return _routine_to_yaml(routine)

    @tool
    async def delete_routine(name: str) -> str:
        """Delete a routine by name for this session."""
        deleted = await svc.delete_routine(session_key, name)
        if deleted:
            return json.dumps({"deleted": name})
        return json.dumps({"error": f"Routine '{name}' not found"})

    return [read_routine, write_routine, delete_routine]
