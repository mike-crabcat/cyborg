"""WebSocket client connecting to the whatsappbridge Go companion service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any
from uuid import uuid4

import websockets

from fastapi import HTTPException

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.models import SessionRouteCreate, SessionRouteKind
from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.session_route_service import SessionRouteService

logger = logging.getLogger(__name__)


def _jid_to_phone(jid: str) -> str:
    """Extract phone number from WhatsApp JID and normalize to +CC format."""
    phone_part = jid.split("@")[0] if "@" in jid else jid
    phone_part = phone_part.split(":")[0] if ":" in phone_part else phone_part
    digits = re.sub(r"\D", "", phone_part)
    if phone_part.startswith("+"):
        return "+" + digits
    # Assume Australian number if no country code
    if digits.startswith("0"):
        return "+61" + digits[1:]
    if digits.startswith("61"):
        return "+" + digits
    if len(digits) > 8:
        return "+" + digits


# The Go bridge has a 1 MB WebSocket read limit.
# base64 adds ~33% overhead, so raw image bytes must stay well under 750 KB.
_BRIDGE_MAX_PAYLOAD_BYTES = 700_000
_WHATSAPP_MAX_DIMENSION = 1920


def _resize_gif(path: str) -> str | None:
    """Downsize an animated GIF by dropping frames and scaling, preserving animation."""
    import tempfile
    from PIL import Image

    img = Image.open(path)
    if not getattr(img, "is_animated", False):
        # Not actually animated — treat as static
        img.close()
        return None

    frames = []
    durations = []
    n_frames = img.n_frames
    # Start by skipping every other frame, increase skip if still too large
    for skip in (1, 2, 3, 4):
        frames.clear()
        durations.clear()
        for i in range(0, n_frames, skip + 1):
            img.seek(i)
            frame = img.copy()
            w, h = frame.size
            if max(w, h) > _WHATSAPP_MAX_DIMENSION:
                ratio = _WHATSAPP_MAX_DIMENSION / max(w, h)
                frame = frame.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            frames.append(frame)
            durations.append(img.info.get("duration", 100))

        if not frames:
            img.close()
            return None

        from io import BytesIO
        buf = BytesIO()
        frames[0].save(
            buf, format="GIF", save_all=True,
            append_images=frames[1:], duration=durations, loop=img.info.get("loop", 0),
            optimize=True,
        )
        for f in frames:
            f.close()
        if buf.tell() <= _BRIDGE_MAX_PAYLOAD_BYTES:
            tmp = tempfile.NamedTemporaryFile(suffix=".gif", delete=False)
            tmp.write(buf.getvalue())
            tmp.close()
            img.close()
            return tmp.name

    img.close()
    return None


async def _prepare_media(path: str) -> str | None:
    """Resize/convert an image to fit WhatsApp and bridge WebSocket limits. Returns path to send."""
    import mimetypes

    mime = (mimetypes.guess_type(path)[0] or "").lower()
    lower_path = path.lower()

    if not mime.startswith("image/") and not lower_path.endswith(".gif"):
        return path

    is_gif = mime == "image/gif" or lower_path.endswith(".gif")
    file_size = os.path.getsize(path)

    # GIFs: if within limits, send as-is to preserve animation
    if is_gif:
        if file_size <= _BRIDGE_MAX_PAYLOAD_BYTES:
            return path
        # Too large — try to reduce frames (returns None for non-animated GIFs)
        import functools
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, functools.partial(_resize_gif, path))
            if result is not None:
                return result
            # Non-animated or resize failed — fall through to static path
        except Exception:
            logger.exception("failed to resize gif %s", path)

    # Static images
    needs_resize = file_size > _BRIDGE_MAX_PAYLOAD_BYTES
    if not needs_resize:
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
            if max(w, h) <= _WHATSAPP_MAX_DIMENSION:
                return path
        except Exception:
            return path

    import functools
    import tempfile

    def _resize() -> str | None:
        from io import BytesIO
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size
        dim = min(max(w, h), _WHATSAPP_MAX_DIMENSION)
        for quality in (85, 70, 55, 40):
            ratio = dim / max(w, h)
            scaled = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = BytesIO()
            scaled.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= _BRIDGE_MAX_PAYLOAD_BYTES:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(buf.getvalue())
                tmp.close()
                img.close()
                return tmp.name
            dim = int(dim * 0.75)

        img.close()
        return None

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(_resize))
    except Exception:
        logger.exception("failed to resize image %s", path)
        return None


class WhatsAppBridgeService(BaseService):
    """WebSocket client connecting to the whatsappbridge Go companion service."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        self._last_bridge_status: dict[str, Any] = {}
        self._last_qr_code: str | None = None
        self._last_pairing_code: str | None = None
        self._subagent_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._subagent_listener_task: asyncio.Task | None = None
        self._presence_subscribed: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        settings = self._get_settings()
        if not settings.whatsapp_bridge.enabled:
            return
        self._task = asyncio.create_task(self._run_loop(), name="whatsapp_bridge")

        # Subscribe to subagent result events and trigger dispatches
        if self.ctx.event_bus:
            self._subagent_queue = self.ctx.event_bus.subscribe()
            self._subagent_listener_task = asyncio.create_task(
                self._subagent_event_loop(), name="subagent_listener"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._subagent_listener_task is not None:
            self._subagent_listener_task.cancel()
            try:
                await self._subagent_listener_task
            except asyncio.CancelledError:
                pass
            self._subagent_listener_task = None
        if self._subagent_queue is not None and self.ctx.event_bus:
            self.ctx.event_bus.unsubscribe(self._subagent_queue)
            self._subagent_queue = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._connected = False

    async def send_message(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str:
        request_id = str(uuid4())
        payload = {
            "type": "send_message",
            "id": request_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to,
                "request_id": request_id,
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("cannot send message, not connected to bridge")
        return request_id

    async def send_media(self, chat_id: str, file_path: str, *, caption: str = "") -> str:
        """Send an image file to a WhatsApp chat."""
        import base64
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        request_id = str(uuid4())
        payload = {
            "type": "send_media",
            "id": request_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "chat_id": chat_id,
                "mime_type": mime,
                "data": data,
                "caption": caption,
                "request_id": request_id,
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("cannot send media, not connected to bridge")
        return request_id

    async def request_pairing(self, *, method: str = "qr", phone_number: str | None = None) -> dict[str, Any]:
        msg_id = str(uuid4())
        payload = {
            "type": "request_pairing",
            "id": msg_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "method": method,
                "phone_number": phone_number or "",
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
            return {"status": "requested", "method": method}
        raise HTTPException(status_code=503, detail="Not connected to bridge")

    async def get_bridge_status(self) -> dict[str, Any]:
        result = {
            "bridge_connected": self._connected,
            **self._last_bridge_status,
            "last_qr_code": self._last_qr_code,
            "last_pairing_code": self._last_pairing_code,
        }
        # Also fetch live pairing info from bridge's HTTP endpoint
        try:
            settings = self._get_settings()
            from urllib.request import urlopen, Request
            bridge_url = settings.whatsapp_bridge.url.replace("ws://", "http://").replace("/ws", "/pairing")
            req = Request(bridge_url)
            with urlopen(req, timeout=5) as resp:
                pairing = json.loads(resp.read())
                if pairing.get("qr_code"):
                    result["last_qr_code"] = pairing["qr_code"]
                if pairing.get("pairing_code"):
                    result["last_pairing_code"] = pairing["pairing_code"]
        except Exception:
            pass
        return result

    async def _run_loop(self) -> None:
        settings = self._get_settings()
        while True:
            try:
                url = settings.whatsapp_bridge.url
                token = settings.whatsapp_bridge.token
                connect_url = f"{url}?token={token}" if token else url

                async with websockets.connect(connect_url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("connected to whatsapp bridge at %s", url)

                    async for raw in ws:
                        try:
                            await self._on_message(json.loads(raw))
                        except Exception:
                            logger.exception("error handling bridge message")

            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                self._ws = None
                logger.warning(
                    "whatsapp bridge connection lost, reconnecting in %ss",
                    settings.whatsapp_bridge.reconnect_interval_seconds,
                    exc_info=True,
                )
                await asyncio.sleep(settings.whatsapp_bridge.reconnect_interval_seconds)

    async def _send_ack(self, message_id: str) -> None:
        if self._ws is None:
            return
        payload = {
            "type": "ack",
            "id": str(uuid4()),
            "timestamp": utcnow().isoformat(),
            "payload": {"message_id": message_id},
        }
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            logger.warning("failed to send ack for %s", message_id, exc_info=True)

    async def _subagent_event_loop(self) -> None:
        """Listen for subagent.result_ready events and trigger dispatches."""
        assert self._subagent_queue is not None
        try:
            while True:
                event = await self._subagent_queue.get()
                event_type = event.get("type", "")
                if event_type != "subagent.result_ready":
                    continue
                payload = event.get("payload", {})
                parent_session_key = payload.get("parent_session_key", "")
                if ":whatsapp:" not in parent_session_key:
                    continue
                try:
                    await self._dispatch_subagent_result(parent_session_key)
                except Exception:
                    logger.exception("failed to dispatch subagent result for %s", parent_session_key)
        except asyncio.CancelledError:
            pass

    async def _dispatch_subagent_result(self, session_key: str) -> None:
        """Dispatch a subagent result into the parent WhatsApp session."""
        settings = self._get_settings()
        if not settings.openai.enabled:
            return

        # Resolve context from session route
        route = await self.db.fetch_one(
            "SELECT channel, kind, contact_id, chat_id, metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if not route or route["channel"] != "whatsapp":
            return

        chat_id = route["chat_id"]
        contact_id = route["contact_id"]
        is_trusted = False
        if contact_id:
            contact = await self.db.fetch_one(
                "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (contact_id,),
            )
            if contact:
                is_trusted = bool(contact.get("is_trusted", 0))

        # Build system prompt
        from cyborg_server.services.session_agenda_service import SessionAgendaService
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages

        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        participants_prompt = await self._build_participants_prompt(session_key)

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, agenda) if p
        )

        # Build tools
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.tool_registry import build_common_tools
        from cyborg_server.services.group_tools import make_group_tools

        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=contact_id)

        # Add group tools if this is a group session
        route_for_kind = await self.db.fetch_one(
            "SELECT kind FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route_for_kind and route_for_kind["kind"] == "group":
            tools.extend(make_group_tools(self.ctx, session_key=session_key))

        wa_service = self
        message_was_sent = [False]
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        async def _run_dispatch() -> str:
            from cyborg_server.services.session_service import SessionService
            from cyborg_server.services.session_dispatch_gate import SessionDispatchGate

            session_svc = SessionService(self.ctx)
            async with SessionDispatchGate.get_lock(session_key):
                claimed = await session_svc.mark_dispatched(session_key)
                if claimed == 0:
                    return ""

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=100,
                )

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="subagent_result",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=contact_id,
                )
                if not message_was_sent[0] and result.strip():
                    from cyborg_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
                        result = await tap_dispatch(
                            self.ctx, messages=messages, tools=tools,
                            session_key=session_key,
                            send_tool_name="send_whatsapp_message",
                            first_result=result,
                            call_category="subagent_result",
                            dispatch_id=dispatch_id,
                            contact_id=contact_id,
                        )

                parts = [p for p in ([result] if result.strip() else []) + sent_texts if p.strip()]
                assistant_text = "\n\n".join(parts) if parts else result
                if not message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    pass
                else:
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp")

                return result

        asyncio.create_task(_run_dispatch())

    async def _build_participants_prompt(self, session_key: str) -> str:
        # For group sessions, use the group members table for richer info
        if ":group:" in session_key:
            route = await self.db.fetch_one(
                "SELECT chat_id FROM session_routes WHERE session_key = ?",
                (session_key,),
            )
            if route and route["chat_id"]:
                group = await self.db.fetch_one(
                    "SELECT id, name, member_count FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
                    (route["chat_id"],),
                )
                if group:
                    members = await self.db.fetch_all(
                        """SELECT gm.display_name, gm.is_admin, gm.is_super_admin,
                                  c.name as contact_name, c.is_trusted
                           FROM whatsappgroup_members gm
                           JOIN contacts c ON c.id = gm.contact_id AND c.deleted_at IS NULL
                           WHERE gm.group_id = ? AND gm.left_at IS NULL
                           ORDER BY gm.is_super_admin DESC, gm.is_admin DESC, gm.display_name ASC""",
                        (group["id"],),
                    )
                    if members:
                        lines = [f"## Participants ({len(members)} members in {group['name'] or 'group'})"]
                        for m in members:
                            name = m["display_name"] or m["contact_name"] or "Unknown"
                            badges = []
                            if m["is_super_admin"]:
                                badges.append("super admin")
                            elif m["is_admin"]:
                                badges.append("admin")
                            trust = "trusted" if m["is_trusted"] else "untrusted"
                            badges.append(trust)
                            lines.append(f"- {name} ({', '.join(badges)})")
                        return "\n".join(lines)

        # Fallback: session_participants for DMs or when group data unavailable
        rows = await self.db.fetch_all(
            "SELECT display_name, identifier, contact_id, is_trusted, last_active_at "
            "FROM session_participants WHERE session_key = ? ORDER BY last_active_at DESC",
            (session_key,),
        )
        if not rows:
            return ""
        lines = ["## Participants"]
        for r in rows:
            name = r["display_name"] or r["identifier"]
            if r["contact_id"]:
                trust = "trusted" if r["is_trusted"] else "untrusted"
                lines.append(f"- {name} (contact, {trust})")
            else:
                lines.append(f"- {name} ({r['identifier']}, not in contacts)")
        return "\n".join(lines)

    async def _resolve_or_seed_contact(self, phone_number: str, display_name: str = "") -> tuple[str, bool]:
        """Find an existing contact by phone or auto-seed an untrusted one. Returns (contact_id, is_trusted)."""
        contact = await self.db.fetch_one(
            "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if contact:
            return contact["id"], bool(contact.get("is_trusted", 0))

        # Fallback: prefix match to catch JIDs with extra trailing digits
        # e.g. +614154068544 should match existing +61415406854
        if len(phone_number) > 6:
            prefix_matches = await self.db.fetch_all(
                "SELECT id, is_trusted, phone_number FROM contacts WHERE deleted_at IS NULL "
                "AND (phone_number = ? OR ? LIKE phone_number || '%' OR phone_number LIKE ? || '%') "
                "ORDER BY LENGTH(phone_number) DESC LIMIT 1",
                (phone_number[:-1], phone_number, phone_number),
            )
            if prefix_matches:
                best = prefix_matches[0]
                logger.info("resolved contact %s via prefix match: %s → %s", best["id"], phone_number, best["phone_number"])
                return best["id"], bool(best.get("is_trusted", 0))
        new_id = str(uuid4())
        now_iso = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (new_id, display_name or phone_number, phone_number, now_iso, now_iso),
        )
        logger.info("auto-seeded untrusted contact %s for phone %s", new_id, phone_number)

        # Auto-create person memory entry
        from cyborg_server.services.memory import MemoryService
        mem_svc = MemoryService(self.ctx)
        mem_svc.ensure_person_entry(
            self.ctx.settings.harness.workspace_dir,
            contact_id=new_id, name=display_name or phone_number,
            phone_number=phone_number, channel="WhatsApp",
        )

        return new_id, False

    async def _handle_group_sync(self, payload: dict[str, Any]) -> None:
        """Handle full group participant sync from bridge (fires on connect for each group)."""
        group_jid = payload.get("group_jid", "")
        group_name = payload.get("group_name", "")
        description = payload.get("description", "")
        participants = payload.get("participants", [])

        if not group_jid:
            return

        logger.info("group sync: %s (%s) with %d participants", group_name, group_jid, len(participants))

        now_iso = utcnow().isoformat()

        # Upsert group
        existing_group = await self.db.fetch_one(
            "SELECT id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (group_jid,),
        )
        if existing_group:
            group_id = existing_group["id"]
            await self.db.execute(
                "UPDATE whatsappgroups SET name = ?, description = ?, member_count = ?, updated_at = ? WHERE id = ?",
                (group_name, description, len(participants), now_iso, group_id),
            )
        else:
            group_id = str(uuid4())
            await self.db.execute(
                """INSERT INTO whatsappgroups (id, whatsapp_jid, name, description, member_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (group_id, group_jid, group_name, description, len(participants), now_iso, now_iso),
            )

        # Process each participant
        seen_contact_ids: set[str] = set()
        for p in participants:
            p_jid = p.get("jid", "")
            phone_number = _jid_to_phone(p_jid)
            display_name = p.get("display_name", "")
            is_admin = 1 if p.get("is_admin") else 0
            is_super_admin = 1 if p.get("is_super_admin") else 0

            contact_id, _ = await self._resolve_or_seed_contact(phone_number, display_name)
            seen_contact_ids.add(contact_id)

            # Upsert group member
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, is_admin, is_super_admin, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       is_admin = excluded.is_admin,
                       is_super_admin = excluded.is_super_admin,
                       display_name = excluded.display_name,
                       left_at = NULL,
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, is_admin, is_super_admin, display_name, now_iso, now_iso, now_iso),
            )

        # Mark departed members
        if seen_contact_ids:
            placeholders = ",".join("?" for _ in seen_contact_ids)
            await self.db.execute(
                f"UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? WHERE group_id = ? AND left_at IS NULL AND contact_id NOT IN ({placeholders})",
                (now_iso, now_iso, group_id, *seen_contact_ids),
            )

        # Upsert all participants into session_participants
        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        for p in participants:
            p_jid = p.get("jid", "")
            phone_number = _jid_to_phone(p_jid)
            display_name = p.get("display_name", "")
            contact = await self.db.fetch_one(
                "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if not contact:
                continue
            await self.db.execute(
                """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_key, identifier) DO UPDATE SET
                       display_name = excluded.display_name,
                       contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                       is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE session_participants.is_trusted END,
                       last_active_at = excluded.last_active_at""",
                (session_key, phone_number, display_name or contact["id"], contact["id"],
                 1 if contact.get("is_trusted") else 0, now_iso),
            )

        # Ensure session route exists
        route_service = SessionRouteService(self.ctx)
        from cyborg_server.exceptions import ConflictError
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=session_key,
                kind=SessionRouteKind.GROUP,
                chat_id=group_jid,
            ))
        except ConflictError:
            pass

    async def _handle_group_member_change(self, payload: dict[str, Any]) -> None:
        """Handle incremental group member join/leave events."""
        group_jid = payload.get("group_jid", "")
        group_name = payload.get("group_name", "")
        sender_jid = payload.get("sender_jid", "")
        joined_jids = payload.get("joined_jids", [])
        left_jids = payload.get("left_jids", [])

        if not group_jid or (not joined_jids and not left_jids):
            return

        logger.info("group member change: %s joined=%d left=%d", group_jid, len(joined_jids), len(left_jids))

        now_iso = utcnow().isoformat()

        # Resolve or create group
        group = await self.db.fetch_one(
            "SELECT id, name FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (group_jid,),
        )
        if not group:
            group_id = str(uuid4())
            await self.db.execute(
                """INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (group_id, group_jid, group_name, now_iso, now_iso),
            )
        else:
            group_id = group["id"]

        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        join_names: list[str] = []
        for jid in joined_jids:
            phone_number = _jid_to_phone(jid)
            # Try to get a display name from existing session_participants or contacts
            display_name = ""
            existing = await self.db.fetch_one(
                "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if existing:
                display_name = existing["name"]

            contact_id, _ = await self._resolve_or_seed_contact(phone_number, display_name)
            join_names.append(display_name or phone_number)

            # Upsert group member (re-join if previously left)
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       left_at = NULL,
                       joined_at = excluded.joined_at,
                       display_name = COALESCE(excluded.display_name, whatsappgroup_members.display_name),
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, display_name, now_iso, now_iso, now_iso),
            )

            # Upsert session participant
            contact = await self.db.fetch_one(
                "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if contact:
                await self.db.execute(
                    """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_key, identifier) DO UPDATE SET
                           display_name = COALESCE(excluded.display_name, session_participants.display_name),
                           contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                           last_active_at = excluded.last_active_at""",
                    (session_key, phone_number, display_name or phone_number, contact["id"],
                     1 if contact.get("is_trusted") else 0, now_iso),
                )

        leave_names: list[str] = []
        for jid in left_jids:
            phone_number = _jid_to_phone(jid)
            existing_contact = await self.db.fetch_one(
                "SELECT id, name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if existing_contact:
                leave_names.append(existing_contact["name"] or phone_number)
                await self.db.execute(
                    "UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? WHERE group_id = ? AND contact_id = ? AND left_at IS NULL",
                    (now_iso, now_iso, group_id, existing_contact["id"]),
                )
            else:
                leave_names.append(phone_number)

        # Update member count
        count_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM whatsappgroup_members WHERE group_id = ? AND left_at IS NULL",
            (group_id,),
        )
        member_count = count_row["cnt"] if count_row else 0
        await self.db.execute(
            "UPDATE whatsappgroups SET member_count = ?, updated_at = ? WHERE id = ?",
            (member_count, now_iso, group_id),
        )

        # Build notification text
        notification_parts = []
        if join_names:
            notification_parts.append(f"Members joined: {', '.join(join_names)}")
        if leave_names:
            notification_parts.append(f"Members left: {', '.join(leave_names)}")
        notification_text = ". ".join(notification_parts)

        # Resolve sender name
        sender_name = ""
        if sender_jid:
            sender_phone = _jid_to_phone(sender_jid)
            sender_contact = await self.db.fetch_one(
                "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (sender_phone,),
            )
            if sender_contact:
                sender_name = sender_contact["name"]

        # Ensure session route exists
        route_service = SessionRouteService(self.ctx)
        from cyborg_server.exceptions import ConflictError
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=session_key,
                kind=SessionRouteKind.GROUP,
                chat_id=group_jid,
            ))
        except ConflictError:
            pass

        # Store notification as user message and dispatch
        settings = self._get_settings()
        if not settings.openai.enabled:
            return

        # Determine trust from session route
        route = await self.db.fetch_one(
            "SELECT contact_id FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        is_trusted = False
        if route and route["contact_id"]:
            contact = await self.db.fetch_one(
                "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (route["contact_id"],),
            )
            if contact:
                is_trusted = bool(contact.get("is_trusted", 0))

        from cyborg_server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        await session_svc.add_message(
            session_key, "user", notification_text,
            channel="whatsapp", sender_id=None, dispatched=0,
        )

        # Build system prompt
        from cyborg_server.services.session_agenda_service import SessionAgendaService
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages

        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=route["contact_id"] if route else None, is_trusted=is_trusted,
        )
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        participants_prompt = await self._build_participants_prompt(session_key)

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt) if p
        )

        # Build tools
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.tool_registry import build_common_tools
        from cyborg_server.services.group_tools import make_group_tools

        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=route["contact_id"] if route else None)
        tools.extend(make_group_tools(self.ctx, session_key=session_key))

        wa_service = self
        chat_id = group_jid
        message_was_sent = [False]
        sent_texts: list[str] = []
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        user_content = "\n".join([
            "## Group Member Change Notification",
            f"Group: {group_name or group_jid}",
            f"Changed by: {sender_name or sender_jid or 'unknown'}",
            "",
            notification_text,
            "",
            "This is a system notification. You do not need to reply unless the member change "
            "is contextually relevant (e.g., greeting a new member, acknowledging a key person leaving). "
            "If no response is needed, call send_whatsapp_message with 'NO_REPLY'.",
        ])
        if agenda:
            user_content = agenda + "\n\n" + user_content

        async def _run_dispatch() -> str:
            from cyborg_server.services.session_dispatch_gate import SessionDispatchGate

            async with SessionDispatchGate.get_lock(session_key):
                claimed = await session_svc.mark_dispatched(session_key)
                if claimed == 0:
                    return ""

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=100,
                )
                # Override the last user message with our notification
                if messages and messages[-1].get("role") == "user":
                    messages[-1]["content"] = user_content

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="whatsapp_group_member_change",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=route["contact_id"] if route else None,
                )
                if not message_was_sent[0] and result.strip():
                    from cyborg_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
                        result = await tap_dispatch(
                            self.ctx, messages=messages, tools=tools,
                            session_key=session_key,
                            send_tool_name="send_whatsapp_message",
                            first_result=result,
                            call_category="whatsapp_group_member_change",
                            dispatch_id=dispatch_id,
                            contact_id=route["contact_id"] if route else None,
                        )

                parts = [p for p in ([result] if result.strip() else []) + sent_texts if p.strip()]
                assistant_text = "\n\n".join(parts) if parts else result
                if not message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    pass
                else:
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp")

                return result

        asyncio.create_task(_run_dispatch())

    async def subscribe_presence(self, chat_id: str) -> None:
        """Request the bridge to subscribe to presence for a chat."""
        payload = {
            "type": "subscribe_presence",
            "id": str(uuid4()),
            "timestamp": utcnow().isoformat(),
            "payload": {"chat_id": chat_id},
        }
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(payload))
            except Exception:
                logger.debug("failed to send presence subscription for %s", chat_id)

    async def _handle_chat_presence(self, payload: dict[str, Any]) -> None:
        """Handle typing/presence events from the bridge."""
        chat_id = payload.get("chat_id", "")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        if not chat_id or not sender_jid:
            return

        chat_kind = "group" if "@g.us" in chat_id else "dm"
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0]
        else:
            key_part = sender_jid.split("@")[0]
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Check if patience is enabled for this session
        route_row = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if not route_row or not route_row["metadata"]:
            return
        try:
            route_meta = json.loads(route_row["metadata"])
        except (json.JSONDecodeError, TypeError):
            return
        if not route_meta.get("patience_enabled"):
            return

        import time as _time
        from cyborg_server.services.patience_buffer import PendingItem, PatienceBufferRegistry

        item = PendingItem(
            item_type="typing",
            timestamp=_time.monotonic(),
            sender_jid=sender_jid,
            sender_name=sender_name or "",
            payload={},
        )
        buffer = PatienceBufferRegistry.get(session_key)

        # Keep only the latest typing event per sender to avoid buffer bloat
        buffer.items = [i for i in buffer.items if i.item_type != "typing" or i.sender_jid != sender_jid]
        buffer.add(item)

        logger.info("patience: typing indicator from %s in %s, buffer=%d messages + %d typing",
                     sender_name, session_key,
                     len([i for i in buffer.items if i.item_type == "message"]),
                     len([i for i in buffer.items if i.item_type == "typing"]))

    async def _handle_slash_command(
        self, text: str, session_key: str, chat_id: str,
        chat_kind: str, sender_jid: str, sender_name: str,
    ) -> None:
        """Handle slash commands from trusted contacts."""
        parts = text.strip().split(None, 1)
        command = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        logger.info("slash command from %s in %s: %s %s", sender_name, session_key, command, args)

        if command == "/patience":
            await self._cmd_patience(args, session_key, chat_id)
        elif command == "/bulletin":
            await self._cmd_bulletin(args, session_key, chat_id, chat_kind)

    async def _cmd_patience(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle patience for the current session."""
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            await self.send_message(chat_id, "Usage: /patience on|off")
            return

        enabled = arg == "on"
        route = await self.db.fetch_one(
            "SELECT id, metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if not route:
            await self.send_message(chat_id, "No session route found")
            return

        meta = json.loads(route["metadata"]) if route["metadata"] else {}
        meta["patience_enabled"] = enabled
        await self.db.execute(
            "UPDATE session_routes SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), utcnow().isoformat(), route["id"]),
        )
        logger.info("patience %s for session %s (route %s)", arg, session_key, route["id"])

        status = "enabled — waiting for silence before responding" if enabled else "disabled — responding immediately"
        await self.send_message(chat_id, f"Patience {status}")

    async def _cmd_bulletin(self, args: str, session_key: str, chat_id: str, chat_kind: str) -> None:
        """Generate bulletins for the current session on demand."""
        from cyborg_server.services.memory import MemoryService

        settings = self._get_settings()
        workspace = settings.harness.workspace_dir
        svc = MemoryService(self.ctx)

        try:
            result = await svc.generate_session_bulletins(
                workspace, session_key, run_dream=True,
            )
        except Exception as exc:
            logger.exception("/bulletin failed for %s", session_key)
            await self.send_message(chat_id, f"/bulletin error: {exc}")
            return

        status = result.get("status", "unknown")
        if status == "empty":
            reason = result.get("reason", "no data")
            await self.send_message(chat_id, f"/bulletin: nothing to process ({reason})")
            return

        n = result.get("bulletins_generated", 0)
        msgs = result.get("messages_processed", 0)
        dream = result.get("dream", {})
        claims = dream.get("claims_extracted", 0) if isinstance(dream, dict) else 0
        entities = dream.get("entity_ops", 0) if isinstance(dream, dict) else 0

        await self.send_message(
            chat_id,
            f"/bulletin: {n} bulletin(s) from {msgs} messages | "
            f"dream: {claims} claims, {entities} entity ops",
        )

    async def _on_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        if msg_type not in ("whatsapp.incoming_message", "whatsapp.message_acked", "bridge.status"):
            logger.info("bridge message: type=%s", msg_type)

        if msg_type == "whatsapp.connected":
            logger.info("whatsapp connected via bridge")
        elif msg_type == "whatsapp.disconnected":
            logger.warning("whatsapp disconnected: %s", payload.get("reason", "unknown"))
        elif msg_type == "whatsapp.qr_code":
            self._last_qr_code = payload.get("qr_string", "")
            logger.info("whatsapp QR code available (expires %s)", payload.get("expires_at", ""))
        elif msg_type == "whatsapp.pairing_code":
            self._last_pairing_code = payload.get("code", "")
            logger.info("whatsapp pairing code: %s", payload.get("code", ""))
        elif msg_type == "whatsapp.incoming_message":
            await self._handle_incoming_message(payload)
        elif msg_type == "whatsapp.message_acked":
            pass
        elif msg_type == "send_message_result":
            if not payload.get("success"):
                logger.warning("send message failed: %s (request %s)", payload.get("error"), payload.get("request_id"))
        elif msg_type == "bridge.status":
            self._last_bridge_status = payload
        elif msg_type == "whatsapp.group_member_change":
            await self._handle_group_member_change(payload)
        elif msg_type == "whatsapp.group_sync":
            await self._handle_group_sync(payload)
        elif msg_type == "whatsapp.chat_presence":
            await self._handle_chat_presence(payload)
        else:
            logger.debug("unknown bridge message type: %s", msg_type)

    async def _handle_incoming_message(self, payload: dict[str, Any]) -> None:
        settings = self._get_settings()
        if not settings.openai.enabled:
            logger.info("No LLM provider configured, skipping dispatch for whatsapp message")
            return

        chat_id = payload.get("chat_id", "")
        chat_kind = payload.get("chat_kind", "dm")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        text = payload.get("text", "")
        wa_message_id = payload.get("whatsapp_message_id", "")
        mentioned_jids = payload.get("mentioned_jids", [])
        media = payload.get("media")

        # Resolve image path from media metadata
        image_path: str | None = None
        image_mime_type: str = "image/jpeg"
        if media and media.get("media_type") == "image":
            media_dir = settings.whatsapp_bridge.media_dir.expanduser().resolve()
            filename = media.get("filename", "")
            if filename:
                resolved = (media_dir / filename).resolve()
                if str(resolved).startswith(str(media_dir)) and resolved.is_file():
                    image_path = str(resolved)
                    image_mime_type = media.get("mime_type", "image/jpeg")

        # Ack receipt so the bridge clears it from the incoming queue
        await self._send_ack(wa_message_id)

        if not text and not image_path:
            return

        logger.info(
            "incoming whatsapp message: chat_id=%s chat_kind=%s sender_jid=%s sender_name=%s",
            chat_id, chat_kind, sender_jid, sender_name,
        )

        # Resolve contact — use chat_id for DMs (sender_jid may be device JID for own messages)
        phone_jid = chat_id if chat_kind == "dm" else sender_jid
        phone_number = _jid_to_phone(phone_jid)
        contact_id = None
        is_trusted = False
        contact = await self.db.fetch_one(
            "SELECT id, name, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if contact:
            contact_id = contact["id"]
            is_trusted = bool(contact.get("is_trusted", 0))
            logger.info("resolved contact %s (trusted=%s) for phone %s", contact_id, is_trusted, phone_number)
            # Backfill name from WhatsApp if contact has no real name
            if sender_name and contact["name"] in ("", phone_number):
                await self.db.execute(
                    "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
                    (sender_name, utcnow().isoformat(), contact_id),
                )
        else:
            logger.info("no contact found for phone %s", phone_number)
            # Auto-seed an untrusted contact for unknown WhatsApp senders
            new_id = str(uuid4())
            now_iso = utcnow().isoformat()
            await self.db.execute(
                """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (new_id, sender_name or phone_number, phone_number, now_iso, now_iso),
            )
            contact_id = new_id
            is_trusted = False
            logger.info("auto-seeded untrusted contact %s for phone %s", contact_id, phone_number)

            # Auto-create person memory entry for new contacts
            from cyborg_server.services.memory import MemoryService
            mem_svc = MemoryService(self.ctx)
            mem_svc.ensure_person_entry(
                settings.harness.workspace_dir,
                contact_id=contact_id, name=sender_name or phone_number,
                phone_number=phone_number, channel="WhatsApp",
            )

        # Derive session key
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0] if "@" in chat_id else chat_id
        else:
            key_part = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Slash command interception — trusted contacts only, never stored or dispatched
        if text.startswith("/"):
            logger.info("slash command intercepted from %s (trusted=%s): %s", sender_name, is_trusted, text[:50])
            if is_trusted:
                await self._handle_slash_command(text, session_key, chat_id, chat_kind, sender_jid, sender_name)
            return

        # Resolve @mentions: replace raw phone numbers with display names
        now_iso = utcnow().isoformat()
        if mentioned_jids and chat_kind == "group":
            mention_map: dict[str, str] = {}
            for jid in mentioned_jids:
                phone = _jid_to_phone(jid)
                # Try session participants first (group members with display names)
                participant = await self.db.fetch_one(
                    "SELECT display_name FROM session_participants WHERE identifier = ? AND session_key = ? LIMIT 1",
                    (phone, session_key),
                )
                if participant and participant["display_name"]:
                    mention_map[phone] = participant["display_name"]
                    continue
                # Then try contacts table
                contact_match = await self.db.fetch_one(
                    "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                    (phone,),
                )
                if contact_match and contact_match["name"]:
                    mention_map[phone] = contact_match["name"]
                # Upsert mentioned user as participant so dispatch-time resolution can find them
                await self.db.execute(
                    """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                       VALUES (?, ?, ?, ?, 0, ?)
                       ON CONFLICT(session_key, identifier) DO UPDATE SET
                           last_active_at = excluded.last_active_at""",
                    (session_key, phone, mention_map.get(phone, phone), None, now_iso),
                )
            # Replace @phone_number patterns with @DisplayName
            for phone, name in mention_map.items():
                bare = phone.lstrip("+")
                text = re.sub(rf"@{re.escape(bare)}\b", f"@{name}", text)

        # Upsert sender as session participant
        await self.db.execute(
            """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_key, identifier) DO UPDATE SET
                   display_name = excluded.display_name,
                   contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                   is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE session_participants.is_trusted END,
                   last_active_at = excluded.last_active_at""",
            (session_key, phone_number, sender_name or phone_number,
             contact_id, 1 if is_trusted else 0, now_iso),
        )

        # Create session route — DM needs contact_id, group needs chat_id
        route_service = SessionRouteService(self.ctx)
        from cyborg_server.exceptions import ConflictError
        try:
            if chat_kind == "group":
                await route_service.create_route(SessionRouteCreate(
                    channel="whatsapp",
                    session_key=session_key,
                    kind=SessionRouteKind.GROUP,
                    chat_id=chat_id,
                    metadata={
                        "sender_jid": sender_jid,
                        "sender_name": sender_name,
                    },
                ))
            else:
                await route_service.create_route(SessionRouteCreate(
                    channel="whatsapp",
                    session_key=session_key,
                    kind=SessionRouteKind.DM,
                    contact_id=contact_id,
                    chat_id=chat_id,
                    metadata={
                        "sender_jid": sender_jid,
                        "sender_name": sender_name,
                    },
                ))
        except ConflictError:
            pass  # Route already exists, proceed with dispatch

        # Resolve agenda
        from cyborg_server.services.session_agenda_service import SessionAgendaService
        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )

        # Build system prompt: workspace context + agenda + participants
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)

        participants_prompt = await self._build_participants_prompt(session_key)

        # Inject person profile for DM sessions
        person_context = ""
        if contact_id and chat_kind != "group":
            from cyborg_server.services.memory import MemoryService
            mem_svc = MemoryService(self.ctx)
            entry = await mem_svc.find_person_entry(
                settings.harness.workspace_dir, contact_id=contact_id,
            )
            if entry:
                person_context = f"## Person Profile\n\n{entry}"

        # Inject group entity hint for group sessions
        group_memory_hint = ""
        if chat_kind == "group":
            group_row = await self.db.fetch_one(
                "SELECT wg.memory_entity_id FROM whatsappgroups wg "
                "JOIN session_routes sr ON sr.chat_id = wg.whatsapp_jid "
                "WHERE sr.session_key = ? AND wg.deleted_at IS NULL",
                (session_key,),
            )
            if group_row and group_row["memory_entity_id"]:
                eid = group_row["memory_entity_id"]
                group_memory_hint = (
                    "## Group Memory\n\n"
                    f"This is a WhatsApp group with accumulated memory entity `{eid}`.\n"
                    f"Use `memory_read('{eid}')` or `memory_graph('{eid}')` to look up group knowledge."
                )

        # Handle shared contacts — auto-seed into contacts table
        shared_contacts = payload.get("contacts", [])
        contacts_block = ""
        if shared_contacts:
            contacts_lines = ["## Shared Contacts"]
            for sc in shared_contacts:
                name = sc.get("display_name", "Unknown")
                phone = sc.get("phone", "")
                vcard = sc.get("vcard", "")
                # Auto-seed contact from shared vCard
                if phone:
                    normalized_phone = _jid_to_phone(phone)
                    existing = await self.db.fetch_one(
                        "SELECT id FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                        (normalized_phone,),
                    )
                    if not existing:
                        new_cid = str(uuid4())
                        await self.db.execute(
                            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
                               VALUES (?, ?, ?, 0, ?, ?)""",
                            (new_cid, name, normalized_phone, now_iso, now_iso),
                        )
                        logger.info("auto-seeded shared contact %s (%s)", name, normalized_phone)
                    contacts_lines.append(f"- **{name}** — {normalized_phone}")
                else:
                    contacts_lines.append(f"- **{name}** (no phone)")
            contacts_block = "\n".join(contacts_lines)

        user_content = "\n".join([
            "## Incoming WhatsApp Message",
            f"From: {sender_name} ({sender_jid})" if sender_name else f"From: {sender_jid}",
            f"Chat: {chat_id} ({chat_kind})",
            f"Message ID: {wa_message_id}",
            "",
            text,
        ])
        if agenda:
            user_content = agenda + "\n\n" + user_content
        if contacts_block:
            user_content += "\n\n" + contacts_block
        user_content += "\n\nRespond to this message by calling send_whatsapp_message with your reply."

        # Store user message immediately so queued messages are visible
        # to the next dispatch that acquires the session lock.
        from cyborg_server.services.session_service import SessionService
        message_metadata: dict[str, Any] | None = None
        if image_path:
            message_metadata = {
                "image_path": image_path,
                "image_mime_type": image_mime_type,
            }
        await SessionService(self.ctx).add_message(
            session_key, "user", text or "[Image]",
            channel="whatsapp", sender_id=contact_id, dispatched=0,
            metadata=message_metadata,
        )

        # Check for active outreach request and inject into system prompt
        outreach_prompt = ""
        route_for_outreach = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route_for_outreach and route_for_outreach["metadata"]:
            try:
                route_meta = json.loads(route_for_outreach["metadata"])
            except (json.JSONDecodeError, TypeError):
                route_meta = {}
            if "outreach_initiated_from" in route_meta:
                outreach_prompt = (
                    "## Active Outreach Request\n"
                    "You proactively sent a message to this contact.\n"
                    f"- Requested by: {route_meta.get('outreach_requestor', 'unknown')}\n"
                    f"- Objective: {route_meta.get('outreach_objective', 'unknown')}\n"
                    f"- Your initial message: \"{route_meta.get('outreach_message', '')}\"\n\n"
                    "Your goal is to achieve the objective through this conversation. "
                    "When you have the information needed, call the finish_outreach tool to relay the result back."
                )

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, person_context, group_memory_hint, outreach_prompt) if p
        )

        logger.info("dispatching whatsapp message session=%s idempotency=%s", session_key, wa_message_id)

        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.tool_registry import build_common_tools
        from cyborg_server.services.group_tools import make_group_tools

        wa_service = self

        # Core tools (workspace, memory, docs, changelog, email_send, contact, phone, reflection, delegation)
        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=contact_id)

        # Group-specific tools
        if chat_kind == "group":
            tools.extend(make_group_tools(self.ctx, session_key=session_key))

        # WhatsApp-specific: outreach tools (trusted DMs and groups)
        if contact_id and (is_trusted or chat_kind == "group"):
            from cyborg_server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
            tools.extend(make_whatsapp_outreach_tools(self.ctx, self, session_key))

        # Outreach reply tool for active outreach targets
        route = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route and route["metadata"]:
            try:
                meta = json.loads(route["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if "outreach_initiated_from" in meta:
                from cyborg_server.services.whatsapp_outreach_tools import make_outreach_reply_tools
                tools.extend(make_outreach_reply_tools(self.ctx, self, session_key))

        message_was_sent = [False]
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str, media_path: str = "") -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            if media_path:
                workspace = settings.harness.workspace_dir.expanduser().resolve()
                resolved = (workspace / media_path).resolve()
                if not str(resolved).startswith(str(workspace)):
                    return "Error: path escapes workspace"
                if not resolved.is_file():
                    return f"Error: file not found: {media_path}"
                prepared = await _prepare_media(str(resolved))
                if prepared is None:
                    return "Error: failed to prepare media for sending"
                sent_texts.append(f"[Image: {text}]" if text else f"[Image: {resolved.name}]")
                request_id = await wa_service.send_media(chat_id, prepared, caption=text)
                return f"Media sent (request_id={request_id})"
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent. "
                "Optionally attach an image or media file by providing media_path."
            ),
            parameters={
                "text": {"type": "string", "description": "The message text to send (used as caption when media_path is provided)."},
                "media_path": {"type": "string", "description": "Optional path to an image or media file, relative to the workspace directory."},
            },
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        async def _run_dispatch() -> str:
            from cyborg_server.services.session_service import SessionService
            from cyborg_server.services.session_dispatch_gate import SessionDispatchGate

            session_svc = SessionService(self.ctx)
            async with SessionDispatchGate.get_lock(session_key):
                claimed = await session_svc.mark_dispatched(session_key)
                if claimed == 0:
                    return ""

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=100,
                )

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="whatsapp_incoming",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=contact_id,
                )
                # Tap: if LLM produced text but didn't use send_whatsapp_message,
                # give it a second chance with a reminder.
                if not message_was_sent[0] and result.strip():
                    from cyborg_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
                        result = await tap_dispatch(
                            self.ctx, messages=messages, tools=tools,
                            session_key=session_key,
                            send_tool_name="send_whatsapp_message",
                            first_result=result,
                            call_category="whatsapp_incoming",
                            dispatch_id=dispatch_id,
                            contact_id=contact_id,
                        )
                # Record to unified session history — combine LLM text output + all sent messages
                # If nothing was sent and the result is just a NO_REPLY variant, skip recording
                # to avoid poisoning future decisions with a pattern of non-responses.
                parts = [p for p in ([result] if result.strip() else []) + sent_texts if p.strip()]
                assistant_text = "\n\n".join(parts) if parts else result
                if not message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    pass  # Don't record NO_REPLY to session history
                else:
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp")
                if self.ctx.event_bus:
                    await self.ctx.event_bus.publish("whatsapp.message.received", {
                        "session_key": session_key,
                        "sender_name": sender_name,
                        "chat_kind": chat_kind,
                        "text_preview": text[:100],
                    })
                return result

        # Check if patience is enabled for this session (per-session via route metadata)
        patience_enabled = False
        route_row = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if route_row and route_row["metadata"]:
            try:
                route_meta = json.loads(route_row["metadata"])
                patience_enabled = route_meta.get("patience_enabled", False)
            except (json.JSONDecodeError, TypeError):
                pass
        logger.info(
            "patience check: session=%s route_found=%s enabled=%s",
            session_key, route_row is not None, patience_enabled,
        )

        if patience_enabled:
            import time as _time
            from cyborg_server.services.patience_buffer import PendingItem
            from cyborg_server.services.patience_gate import submit_to_patience

            item = PendingItem(
                item_type="message",
                timestamp=_time.monotonic(),
                sender_jid=sender_jid,
                sender_name=sender_name or "",
                payload={"text": text},
            )
            await submit_to_patience(
                self.ctx, session_key, item, _run_dispatch,
                bot_name=settings.patience.bot_name,
                model=settings.patience.model,
                max_pending_items=settings.patience.max_pending_items,
                max_context_messages=settings.patience.max_context_messages,
            )

            # Auto-subscribe to presence for this chat
            if chat_id not in self._presence_subscribed:
                await self.subscribe_presence(chat_id)
                self._presence_subscribed.add(chat_id)
        else:
            asyncio.create_task(_run_dispatch())
