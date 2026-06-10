"""Unified LLM dispatch service — logs all LLM interactions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4


def _content_char_len(content: Any) -> int:
    """Return character length of message content, handling both str and list[dict]."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get("text", "")) if isinstance(part, dict) else 0
            for part in content
        )
    return 0

from cyborg_server.services.tools import Tool

from cyborg_server.services.base import BaseService
from cyborg_server.services.openai_service import OpenAIService, StreamResult

logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively convert non-serializable objects to plain dicts."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if hasattr(obj, "model_dump"):
        return _sanitize_for_json(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return _sanitize_for_json(vars(obj))
    return str(obj)


def _extract_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, str]:
    """Extract system_prompt and user_message from a messages array."""
    system_prompt = ""
    user_message = ""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            system_prompt = content if isinstance(content, str) else ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            user_message = content if isinstance(content, str) else ""
            break
    return system_prompt, user_message


async def _record_log(
    db: Any,
    *,
    log_id: str | None = None,
    provider: str = "",
    model: str = "",
    call_category: str = "",
    session_key: str | None = None,
    system_prompt: str = "",
    user_message: str = "",
    messages_json: str | None = None,
    tools_json: str | None = None,
    response_text: str = "",
    latency_seconds: float | None = None,
    ttft_seconds: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    status: str = "completed",
    error_message: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    dispatch_id: str | None = None,
    contact_id: str | None = None,
) -> str:
    """Record or update an LLM call log entry. Returns the log_id.

    If log_id is provided and a row with that id exists, UPDATE it.
    Otherwise INSERT a new row.
    """
    try:
        if log_id is not None:
            existing = await db.fetch_one(
                "SELECT id FROM llm_call_log WHERE id = ?", (log_id,),
            )
            if existing:
                await db.execute(
                    """UPDATE llm_call_log SET
                       response_text=?, latency_seconds=?, ttft_seconds=?,
                       prompt_tokens=?, completion_tokens=?, total_tokens=?, cached_tokens=?,
                       status=?, error_message=?, messages_json=COALESCE(?, messages_json)
                       WHERE id = ?""",
                    (
                        response_text, latency_seconds, ttft_seconds,
                        prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                        status, error_message, messages_json,
                        log_id,
                    ),
                )
                return log_id

        row_id = log_id or str(uuid4())
        await db.execute(
            """INSERT INTO llm_call_log
               (id, provider, model, call_category, session_key,
                system_prompt, user_message, messages_json, tools_json,
                response_text, latency_seconds, ttft_seconds,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                status, error_message, project_id, task_id, dispatch_id, contact_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id, provider, model, call_category, session_key,
                system_prompt, user_message, messages_json, tools_json,
                response_text, latency_seconds, ttft_seconds,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                status, error_message, project_id, task_id, dispatch_id, contact_id,
            ),
        )
        return row_id
    except Exception:
        logger.warning("Failed to record LLM call log", exc_info=True)
        return log_id or str(uuid4())


class LLMDispatchService(BaseService):
    """Routes LLM calls to OpenAI and logs all interactions."""

    async def _publish_call(self, *, status: str, session_key: str | None,
                            call_category: str, model: str, latency_seconds: float | None,
                            total_tokens: int | None, **kwargs: Any) -> None:
        if self.ctx.event_bus is None:
            return
        event_type = f"llm.call.{status}"
        await self.ctx.event_bus.publish(event_type, {
            "session_key": session_key,
            "call_category": call_category,
            "model": model,
            "status": status,
            "latency_seconds": latency_seconds,
            "total_tokens": total_tokens,
            **kwargs,
        })

    def _get_service(self) -> OpenAIService:
        return OpenAIService(self.ctx)

    def _make_tool_callback(self, session_key: str | None, call_category: str, log_id: str | None = None) -> Any:
        async def _on_tool_call(name: str, args: dict, result_summary: str) -> None:
            if self.ctx.event_bus is None:
                return
            payload: dict[str, Any] = {
                "session_key": session_key,
                "call_category": call_category,
                "tool_name": name,
                "tool_args": args,
                "tool_output": result_summary,
            }
            if log_id:
                payload["log_id"] = log_id
            await self.ctx.event_bus.publish("llm.call.tool_completed", payload)
        return _on_tool_call

    def _resolve_model(self, model: str | None = None) -> str:
        if model:
            return model
        return self._get_settings().openai.default_model

    @property
    def memory_model(self) -> str:
        return self._get_settings().openai.get_memory_model()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> str:
        """Non-streaming chat completion with automatic logging."""
        resolved_model = self._resolve_model(model)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)
        t0 = time.monotonic()

        try:
            stream_result = StreamResult()
            result = await service.chat(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream_result=stream_result,
            )
            elapsed = time.monotonic() - t0

            await _record_log(
                self.db,
                provider="openai",
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=result or "",
                latency_seconds=elapsed,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )

            logger.info(
                "LLM dispatch: model=%s category=%s latency=%.2fs "
                "input_chars=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed,
                sum(_content_char_len(m.get("content", "")) for m in messages),
                len(result or ""),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("LLM dispatch failed: model=%s error=%s", resolved_model, exc)
            await _record_log(
                self.db,
                provider="openai",
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                latency_seconds=elapsed,
                status="failed",
                error_message=str(exc),
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion with automatic logging."""
        resolved_model = self._resolve_model(model)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)

        stream_result = StreamResult()
        t0 = time.monotonic()
        ttft: float | None = None
        accumulated = ""

        try:
            async for chunk in service.chat_stream(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream_result=stream_result,
            ):
                if chunk:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    accumulated += chunk
                    yield chunk

            elapsed = time.monotonic() - t0

            await _record_log(
                self.db,
                provider="openai",
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=accumulated,
                latency_seconds=elapsed,
                ttft_seconds=ttft,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )

            logger.info(
                "LLM dispatch stream: model=%s category=%s latency=%.2fs ttft=%.2fs "
                "input_chars=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed, ttft or 0,
                sum(_content_char_len(m.get("content", "")) for m in messages),
                len(accumulated),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
                ttft_seconds=ttft,
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("LLM dispatch stream failed: model=%s error=%s", resolved_model, exc)
            await _record_log(
                self.db,
                provider="openai",
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=accumulated,
                latency_seconds=elapsed,
                ttft_seconds=ttft,
                status="failed",
                error_message=str(exc),
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        model: str | None = None,
        max_iterations: int = 100,
        call_category: str = "tool_call",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> str:
        """Chat with tool calling. Loops until LLM finishes or max iterations.

        The caller provides a list of Tool objects (created via @tool decorator)
        and this method handles the multi-turn tool call loop automatically.
        """
        resolved_model = self._resolve_model(model)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)

        openai_tools = [t.to_openai_format() for t in tools]
        tool_handlers = {t.name: t.handler for t in tools}
        tools_json = json.dumps(openai_tools) if openai_tools else None

        t0 = time.monotonic()
        log_id = await _record_log(
            self.db,
            provider="openai", model=resolved_model,
            call_category=call_category, session_key=session_key,
            system_prompt=system_prompt, user_message=user_message,
            messages_json=json.dumps(_sanitize_for_json(messages)),
            tools_json=tools_json,
            status="running",
            project_id=project_id, task_id=task_id,
            dispatch_id=dispatch_id, contact_id=contact_id,
        )
        await self._publish_call(
            status="running", session_key=session_key,
            call_category=call_category, model=resolved_model,
            latency_seconds=None, total_tokens=None,
            log_id=log_id,
        )
        try:
            stream_result = StreamResult()

            async def _on_iteration(msgs: list[dict[str, Any]]) -> None:
                await _record_log(self.db, log_id=log_id,
                    messages_json=json.dumps(_sanitize_for_json(msgs)),
                    status="running",
                )

            result = await service.chat_with_tools(
                messages=messages,
                tools=openai_tools,
                tool_handlers=tool_handlers,
                model=resolved_model,
                max_iterations=max_iterations,
                stream_result=stream_result,
                on_tool_call=self._make_tool_callback(session_key, call_category, log_id),
                on_iteration_complete=_on_iteration,
            )
            elapsed = time.monotonic() - t0

            await _record_log(self.db, log_id=log_id,
                response_text=result,
                latency_seconds=elapsed,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                status="completed",
            )

            logger.info(
                "LLM dispatch tools: model=%s category=%s latency=%.2fs "
                "tools=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed,
                len(tools), len(result),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
            )
            return result

        except BaseException as exc:
            elapsed = time.monotonic() - t0
            is_cancel = isinstance(exc, asyncio.CancelledError)
            if not is_cancel:
                logger.error("LLM dispatch tools failed: model=%s error=%s", resolved_model, exc)
            await _record_log(self.db, log_id=log_id,
                latency_seconds=elapsed,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                status="failed",
                error_message=f"Cancelled — server restart" if is_cancel else str(exc),
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        model: str | None = None,
        max_iterations: int = 100,
        call_category: str = "voice_chat",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream chat with tool calling support.

        Handles the tool loop non-streamingly (tool calls need full responses),
        then streams the final text response for real-time TTS/consumption.
        """
        resolved_model = self._resolve_model(model)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)

        openai_tools = [t.to_openai_format() for t in tools]
        tool_handlers = {t.name: t.handler for t in tools}
        tools_json = json.dumps(openai_tools) if openai_tools else None

        t0 = time.monotonic()
        log_id = await _record_log(
            self.db,
            provider="openai", model=resolved_model,
            call_category=call_category, session_key=session_key,
            system_prompt=system_prompt, user_message=user_message,
            messages_json=json.dumps(_sanitize_for_json(messages)),
            tools_json=tools_json,
            status="running",
            project_id=project_id, task_id=task_id,
            dispatch_id=dispatch_id, contact_id=contact_id,
        )
        await self._publish_call(
            status="running", session_key=session_key,
            call_category=call_category, model=resolved_model,
            latency_seconds=None, total_tokens=None,
            log_id=log_id,
        )
        accumulated = ""
        ttft: float | None = None

        async def _on_iteration(msgs: list[dict[str, Any]]) -> None:
            await _record_log(self.db, log_id=log_id,
                messages_json=json.dumps(_sanitize_for_json(msgs)),
                status="running",
            )

        try:
            async for chunk in service.chat_stream_with_tools(
                messages=messages,
                tools=openai_tools,
                tool_handlers=tool_handlers,
                model=resolved_model,
                max_iterations=max_iterations,
                on_tool_call=self._make_tool_callback(session_key, call_category, log_id),
                on_iteration_complete=_on_iteration,
            ):
                if chunk:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    accumulated += chunk
                    yield chunk

            await _record_log(self.db, log_id=log_id,
                response_text=accumulated,
                latency_seconds=time.monotonic() - t0,
                ttft_seconds=ttft,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                status="completed",
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=time.monotonic() - t0, total_tokens=None,
                ttft_seconds=ttft,
            )

        except BaseException as exc:
            is_cancel = isinstance(exc, asyncio.CancelledError)
            if not is_cancel:
                logger.error("LLM dispatch stream+tools failed: model=%s error=%s", resolved_model, exc)
            await _record_log(self.db, log_id=log_id,
                response_text=accumulated,
                latency_seconds=time.monotonic() - t0,
                ttft_seconds=ttft,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                status="failed",
                error_message=f"Cancelled — server restart" if is_cancel else str(exc),
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=time.monotonic() - t0, total_tokens=None,
                error_message=str(exc),
            )
            raise
