"""Outbound email delivery via AgentMail."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.services.agentmail_client import AgentMailClient
from cyborg_server.services.base import BaseService, json_dumps, utcnow


logger = logging.getLogger(__name__)


class EmailDeliveryService(BaseService):
    """Send outgoing email via AgentMail."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        agentmail_client: AgentMailClient | None = None,
    ) -> None:
        super().__init__(ctx)
        self._client = agentmail_client

    @property
    def client(self) -> AgentMailClient:
        if self._client is None:
            settings = self._get_settings()
            self._client = AgentMailClient(
                base_url=settings.agentmail.base_url,
                api_key=settings.agentmail.api_key,
            )
        return self._client

    async def send_reply(
        self,
        *,
        inbox_id: str,
        thread_id: str,
        text: str,
        html: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a reply in an existing email thread.

        thread_id is the agentmail_thread_id (not the local email_threads.id).
        Finds the latest message in the thread and replies to it.
        Falls back to sending a new threaded message if no existing message found.
        """
        # Look up the latest message in this thread to get its agentmail_message_id
        latest = await self.db.fetch_one(
            """
            SELECT em.agentmail_message_id, em.inbox_id
            FROM email_messages em
            WHERE em.thread_id = ?
            ORDER BY em.message_timestamp DESC
            LIMIT 1
            """,
            (thread_id,),
        )

        inbox = await self.db.fetch_one(
            "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
            (inbox_id,),
        )
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found")

        agentmail_inbox_id = inbox["agentmail_inbox_id"]

        result: dict[str, Any] = {}
        if latest is not None and latest["agentmail_message_id"]:
            try:
                result = await self.client.reply_message(
                    agentmail_inbox_id,
                    latest["agentmail_message_id"],
                    text=text,
                    html=html,
                    reply_all=True,
                    attachments=attachments,
                )
            except Exception:
                logger.warning(
                    "Failed to reply to message %s, falling back to threaded send",
                    latest["agentmail_message_id"],
                    exc_info=True,
                )
                result = {}

        if not result:
            result = await self.client.send_message(
                agentmail_inbox_id,
                to="",  # thread_id handles routing
                subject="",
                text=text,
                html=html,
                thread_id=thread_id,
                attachments=attachments,
            )

        await self._persist_sent_message(
            inbox=inbox,
            agentmail_response=result,
            agentmail_thread_id=thread_id,
            text=text,
            html=html,
            has_attachments=bool(attachments),
        )
        return result

    async def send_new_email(
        self,
        *,
        inbox_id: str,
        to: str | list[str],
        subject: str,
        text: str,
        html: str | None = None,
        cc: list[str] | None = None,
        agenda: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        origin_session_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a new email, create thread, persist message, and prime LLM context.

        Returns the AgentMail response dict.
        """
        inbox = await self.db.fetch_one(
            "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL AND is_active = 1",
            (inbox_id,),
        )
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found or inactive")

        result = await self.client.send_message(
            inbox["agentmail_inbox_id"],
            to=to,
            subject=subject,
            text=text,
            html=html,
            cc=cc,
            attachments=attachments,
        )

        agentmail_thread_id = result.get("thread_id", "")
        if not agentmail_thread_id:
            return result

        # Look up contact from recipient
        contact_id = None
        recipient_email = to if isinstance(to, str) else (to[0] if to else "")
        if recipient_email:
            contact = await self.db.fetch_one(
                "SELECT id FROM contacts WHERE email = ? AND deleted_at IS NULL LIMIT 1",
                (recipient_email,),
            )
            if contact:
                contact_id = contact["id"]

        # Create thread + session route
        from cyborg_server.services.email_polling_service import (
            CUSTOM_AGENDA_TEMPLATE,
            resolve_or_create_email_thread,
        )
        thread, is_new_thread = await resolve_or_create_email_thread(
            self.db,
            inbox=inbox,
            agentmail_thread_id=agentmail_thread_id,
            subject=subject,
            contact_id=contact_id,
            agenda=agenda,
            origin_session_key=origin_session_key,
        )

        # Persist agenda to session_agendas immediately (not waiting for lazy migration)
        if agenda and thread:
            from cyborg_server.services.session_agenda_service import SessionAgendaService
            await SessionAgendaService(self.ctx).set_agenda(thread["session_key"], agenda)

        # Persist outgoing message
        await self._persist_sent_message(
            inbox=inbox,
            agentmail_response=result,
            agentmail_thread_id=agentmail_thread_id,
            text=text,
            html=html,
            subject=subject,
            to_addresses=[to] if isinstance(to, str) else to,
            cc_addresses=cc,
            has_attachments=bool(attachments),
        )

        # Dispatch to LLM for context priming
        settings = self._get_settings()
        if settings.openai.enabled:
            from cyborg_server.services.llm_dispatch import LLMDispatchService
            from cyborg_server.services.session_service import SessionService
            from cyborg_server.services.prompt_assembler import load_workspace_prompt

            send_content = "\n".join([
                "## Email You Just Sent",
                "This is provided for your context. Do NOT reply — wait for the recipient to respond.",
                "",
                f"Subject: {subject}",
                f"To: {to}",
                "",
                text,
            ])

            session_key = thread["session_key"]
            logger.info("Dispatching send to LLM session=%s new_thread=%s", session_key, is_new_thread)

            workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
            custom_agenda = CUSTOM_AGENDA_TEMPLATE.format(agenda=agenda) if is_new_thread and agenda else None
            system_parts = [p for p in (
                workspace_prompt,
                custom_agenda,
                "You are managing an email conversation. The following is an outgoing email you sent for your context.",
            ) if p]

            messages = [
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": send_content},
            ]

            dispatch_id = str(uuid4())
            ctx = self.ctx
            email_text = text

            async def _run_send_dispatch() -> str:
                result = await LLMDispatchService(ctx).chat_with_tools(
                    messages, [],
                    call_category="email_outgoing",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                )
                session_svc = SessionService(ctx)
                await session_svc.add_message(session_key, "assistant", email_text, channel="email")
                return result

            asyncio.create_task(_run_send_dispatch())
            logger.info("Send dispatch tracking for thread %s (dispatch=%s)", thread["id"], dispatch_id)

        return result

    async def _persist_sent_message(
        self,
        *,
        inbox: dict[str, Any],
        agentmail_response: dict[str, Any],
        agentmail_thread_id: str,
        text: str,
        html: str | None = None,
        subject: str | None = None,
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        has_attachments: bool = False,
    ) -> str:
        """Persist a sent message to email_messages and update thread stats."""
        agentmail_message_id = agentmail_response.get("message_id", "")
        if not agentmail_message_id:
            logger.warning("No message_id in AgentMail response, skipping persistence")
            return ""

        now = utcnow()
        message_id = str(uuid4())

        await self.db.execute(
            """
            INSERT INTO email_messages (
                id, inbox_id, agentmail_message_id, thread_id,
                subject, sender_email, sender_name,
                to_addresses, cc_addresses,
                text_body, html_body, preview, labels,
                has_attachments, in_reply_to,
                message_timestamp, processed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                inbox["id"],
                agentmail_message_id,
                agentmail_thread_id,
                subject,
                inbox["email_address"],
                inbox.get("display_name"),
                json_dumps(to_addresses or []),
                json_dumps(cc_addresses or []),
                text,
                html,
                text[:200] if text else None,
                json_dumps(["sent"]),
                1 if has_attachments else 0,
                None,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )

        # Update thread message count
        await self.db.execute(
            """
            UPDATE email_threads
            SET message_count = message_count + 1, last_message_at = ?, updated_at = ?
            WHERE agentmail_thread_id = ? AND deleted_at IS NULL
            """,
            (now.isoformat(), now.isoformat(), agentmail_thread_id),
        )

        logger.info(
            "Persisted sent message %s in thread %s",
            message_id, agentmail_thread_id,
        )
        return message_id
