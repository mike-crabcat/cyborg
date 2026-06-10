"""Local LLM harness that assembles prompts from workspace files and dispatches to LLM."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Any

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)


class HarnessService(BaseService):
    """Assembles system prompts from workspace files and dispatches via LLMDispatchService."""

    async def stream_chat(
        self,
        message: str,
        session_key: str,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_start: Callable[[], Awaitable[None]] | None = None,
        model: str | None = None,
        voice_instructions: str = "",
        dispatch_id: str | None = None,
    ) -> str:
        """Stream a chat completion with assembled system prompt."""
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        settings = self._get_settings()
        resolved_model = model or settings.harness.default_model

        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        messages = await build_chat_messages(
            message, session_key,
            db=self.db,
            system_content=workspace_prompt,
            voice_instructions=voice_instructions,
            max_history=settings.harness.max_history_messages,
        )

        dispatch = LLMDispatchService(self.ctx)
        accumulated = ""

        async for chunk in dispatch.chat_stream(
            messages,
            model=resolved_model,
            call_category="voice_chat",
            session_key=session_key,
            dispatch_id=dispatch_id,
        ):
            if chunk:
                accumulated += chunk
                if on_delta:
                    await on_delta(accumulated)

        return accumulated

    async def chat(
        self,
        message: str,
        session_key: str = "",
        *,
        model: str | None = None,
        voice_instructions: str = "",
    ) -> str:
        """Non-streaming chat completion with assembled system prompt."""
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        settings = self._get_settings()
        resolved_model = model or settings.harness.default_model

        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        messages = await build_chat_messages(
            message, session_key,
            db=self.db if session_key else None,
            system_content=workspace_prompt,
            voice_instructions=voice_instructions,
            max_history=settings.harness.max_history_messages,
        )

        dispatch = LLMDispatchService(self.ctx)
        return await dispatch.chat(
            messages,
            model=resolved_model,
            call_category="voice_chat",
            session_key=session_key or None,
        )
