"""Subagent tools — let Cyborg's LLM manage async subagents."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def make_subagent_tools(ctx: AppContext, session_key: str) -> list:
    """Create subagent management tools for a trusted session."""

    @tool
    async def create_subagent(task: str, agent_type: str = "claude", persona: bool = False, model: str = "") -> str:
        """Spawn a subagent to work on a task asynchronously. Returns subagent_id immediately.
        agent_type: 'claude' (spawns Claude CLI subprocess) or 'local' (runs in-process, faster).
        persona: if true and local, load full agent persona; if false, uses minimal system prompt.
        model: override model for local subagents (default: gpt-5.5).
        After calling this, you MUST send a message to the user summarizing what you delegated.
        Use check_subagent to poll for results and message_subagent for follow-up."""
        from cyborg_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.create_subagent(task, session_key, agent_type=agent_type, persona=persona, model=model)
        return json.dumps(result)

    @tool
    async def check_subagent(subagent_id: str) -> str:
        """Check the status and result of a subagent. Returns current status and result if available."""
        from cyborg_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.check_subagent(subagent_id)
        return json.dumps(result)

    @tool
    async def message_subagent(subagent_id: str, message: str) -> str:
        """Send a follow-up message to a subagent. The subagent will process your message
        and return a response. Only use on subagents in 'waiting_for_parent' status."""
        from cyborg_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.message_subagent(subagent_id, message)
        return json.dumps(result)

    @tool
    async def list_subagents(status: str = "") -> str:
        """List your subagents, optionally filtered by status.
        Valid statuses: created, running, waiting_for_parent, completed, failed, killed."""
        from cyborg_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        results = await svc.list_subagents(session_key, status)
        return json.dumps(results)

    @tool
    async def kill_subagent(subagent_id: str) -> str:
        """Kill a running subagent. Cancels execution and marks it as killed."""
        from cyborg_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.kill_subagent(subagent_id)
        return json.dumps(result)

    return [create_subagent, check_subagent, message_subagent, list_subagents, kill_subagent]
