"""Email tools for LLM function calling."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

from cyborg_server.context import AppContext
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB


def _read_file_as_attachment(
    file_path: str,
    workspace_dir: Path,
) -> dict:
    """Read a file and return an attachment dict for the delivery service."""
    path = Path(file_path)
    if not path.is_absolute():
        path = workspace_dir / path
    path = path.resolve()

    if not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not str(path).startswith(str(workspace_dir.resolve())):
        raise ValueError(f"File must be within the workspace: {file_path}")

    size = path.stat().st_size
    if size > MAX_ATTACHMENT_SIZE:
        raise ValueError(f"File too large ({size} bytes, max {MAX_ATTACHMENT_SIZE}): {file_path}")

    content = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "content": content,
        "filename": path.name,
        "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
    }


def make_email_tools(
    ctx: AppContext,
    thread_id: str,
    inbox_id: str,
    *,
    reply_tracker: list | None = None,
    reply_body_tracker: list | None = None,
    inbox_agentmail_id: str = "",
    is_trusted: bool = False,
):
    """Create email reply/skip tools bound to the given thread.

    If reply_tracker is provided, email_reply sets tracker[0] = True
    so callers can detect whether a reply was sent.
    If reply_body_tracker is provided, email_reply appends the body text.
    """

    @tool
    async def email_reply(body: str, attachments: list[str] | None = None) -> str:
        """Send a reply to the current email thread. Always use this tool to respond — do not just generate text output. Optionally attach files by providing their paths as a list (workspace-relative or absolute)."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        if isinstance(attachments, str):
            s = attachments.strip()
            attachments = None if not s or s == "[]" else [s]

        settings = ctx.settings
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()

        attachment_dicts = None
        if attachments:
            attachments = [fp for fp in attachments if fp and fp.strip()]
        if attachments:
            attachment_dicts = []
            errors = []
            for fp in attachments:
                try:
                    attachment_dicts.append(_read_file_as_attachment(fp, workspace_dir))
                except (FileNotFoundError, ValueError) as e:
                    errors.append(str(e))
            if errors:
                return f"Error with attachments: {'; '.join(errors)}"

        svc = EmailDeliveryService(ctx)
        try:
            await svc.send_reply(
                inbox_id=inbox_id,
                thread_id=thread_id,
                text=body,
                attachments=attachment_dicts,
            )
            if reply_tracker is not None:
                reply_tracker[0] = True
            if reply_body_tracker is not None:
                reply_body_tracker.append(body)
            result = {"ok": True, "thread_id": thread_id}
            if attachment_dicts:
                result["attachments_sent"] = [a["filename"] for a in attachment_dicts]
            return json.dumps(result)
        except Exception as e:
            logger.warning("email_reply failed: %s", e)
            return f"Error sending reply: {e}"

    @tool
    async def email_skip() -> str:
        """Skip replying to this email — no response is needed."""
        return json.dumps({"ok": True, "skipped": True})

    @tool
    async def list_attachments() -> str:
        """List all attachments across all messages in this email thread.
        Shows filename, content type, size, download status, and attachment_id.
        Use download_attachment with the attachment_id to save a file to the workspace."""
        messages = await ctx.db.fetch_all(
            "SELECT agentmail_message_id, sender_email, sender_name, "
            "subject, message_timestamp, attachments_json "
            "FROM email_messages "
            "WHERE thread_id = ? AND has_attachments = 1 "
            "ORDER BY message_timestamp ASC",
            (thread_id,),
        )

        if not messages:
            return json.dumps({"attachments": [], "message": "No attachments found in this thread"})

        all_attachments = []
        for msg in messages:
            att_json = msg.get("attachments_json")
            if not att_json:
                continue
            try:
                attachments = json.loads(att_json)
            except (ValueError, TypeError):
                continue

            for att in attachments:
                all_attachments.append({
                    "attachment_id": att.get("attachment_id", ""),
                    "filename": att.get("filename", ""),
                    "content_type": att.get("content_type", ""),
                    "size": att.get("size"),
                    "downloaded": att.get("downloaded", False),
                    "path": att.get("path"),
                    "from_message": {
                        "sender": msg.get("sender_name") or msg.get("sender_email"),
                        "subject": msg.get("subject"),
                        "timestamp": msg.get("message_timestamp"),
                    },
                })

        return json.dumps({
            "thread_id": thread_id,
            "total_attachments": len(all_attachments),
            "can_download": is_trusted,
            "attachments": all_attachments,
        })

    @tool
    async def download_attachment(attachment_id: str) -> str:
        """Download an email attachment to the workspace directory.
        Use list_attachments first to find the attachment_id. Only available for trusted senders."""
        if not is_trusted:
            return "Error: Attachment downloads are not available for untrusted senders."

        if not attachment_id:
            return "Error: attachment_id is required."

        # Find the message that owns this attachment_id
        messages = await ctx.db.fetch_all(
            "SELECT agentmail_message_id, attachments_json "
            "FROM email_messages "
            "WHERE thread_id = ? AND has_attachments = 1",
            (thread_id,),
        )

        target_msg_id = None
        target_att = None
        for msg in messages:
            att_json = msg.get("attachments_json")
            if not att_json:
                continue
            try:
                attachments = json.loads(att_json)
            except (ValueError, TypeError):
                continue
            for att in attachments:
                if att.get("attachment_id") == attachment_id:
                    target_msg_id = msg["agentmail_message_id"]
                    target_att = att
                    break
            if target_msg_id:
                break

        if not target_msg_id or not target_att:
            return f"Error: Attachment {attachment_id} not found in this thread."

        # Already downloaded?
        if target_att.get("downloaded") and target_att.get("path"):
            existing_path = Path(target_att["path"])
            if existing_path.exists():
                return json.dumps({
                    "ok": True,
                    "path": target_att["path"],
                    "filename": target_att["filename"],
                    "message": "Already downloaded",
                })

        # Download via AgentMail
        from cyborg_server.services.agentmail_client import AgentMailClient

        settings = ctx.settings
        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )

        try:
            content = await client.get_attachment(
                inbox_agentmail_id,
                target_msg_id,
                attachment_id,
            )
        except Exception as e:
            logger.warning("Failed to download attachment %s: %s", attachment_id, e)
            return f"Error downloading attachment: {e}"
        finally:
            await client.close()

        # Save to workspace
        filename = target_att.get("filename", attachment_id)
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        dest_dir = workspace_dir / "attachments" / thread_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        # Avoid overwriting by appending suffix if needed
        counter = 1
        base_dest = dest
        while dest.exists():
            stem = base_dest.stem
            suffix = base_dest.suffix
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        dest.write_bytes(content)

        # Update attachments_json to mark as downloaded
        msg_row = await ctx.db.fetch_one(
            "SELECT attachments_json FROM email_messages WHERE agentmail_message_id = ?",
            (target_msg_id,),
        )
        if msg_row and msg_row.get("attachments_json"):
            try:
                atts = json.loads(msg_row["attachments_json"])
                for att in atts:
                    if att.get("attachment_id") == attachment_id:
                        att["downloaded"] = True
                        att["path"] = str(dest)
                        att["size"] = len(content)
                await ctx.db.execute(
                    "UPDATE email_messages SET attachments_json = ? WHERE agentmail_message_id = ?",
                    (json.dumps(atts), target_msg_id),
                )
            except (ValueError, TypeError):
                pass

        return json.dumps({
            "ok": True,
            "filename": filename,
            "size": len(content),
            "path": str(dest),
        })

    return [email_reply, email_skip, list_attachments, download_attachment]


def make_email_send_tools(ctx: AppContext, *, session_key: str | None = None) -> list[Tool]:
    """Create email_send tool for initiating new email threads. Not bound to a specific thread."""

    @tool
    async def email_send(
        to: str,
        subject: str,
        body: str,
        agenda: str,
        attachments: list[str] | None = None,
    ) -> str:
        """Send a new email to start a conversation with someone. Use this to proactively reach out to a contact by email (follow up, schedule, begin a discussion). The agenda describes the purpose and guides all future responses in this thread. The recipient email address must be known. Optionally attach files by providing their paths as a list (workspace-relative or absolute)."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        if isinstance(attachments, str):
            s = attachments.strip()
            attachments = None if not s or s == "[]" else [s]

        settings = ctx.settings
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()

        attachment_dicts = None
        if attachments:
            attachments = [fp for fp in attachments if fp and fp.strip()]
        if attachments:
            attachment_dicts = []
            errors = []
            for fp in attachments:
                try:
                    attachment_dicts.append(_read_file_as_attachment(fp, workspace_dir))
                except (FileNotFoundError, ValueError) as e:
                    errors.append(str(e))
            if errors:
                return f"Error with attachments: {'; '.join(errors)}"

        # Resolve default inbox
        inbox = await ctx.db.fetch_one(
            "SELECT id FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1 LIMIT 1",
        )
        if inbox is None:
            return "Error: no active email inbox configured"

        try:
            svc = EmailDeliveryService(ctx)
            result = await svc.send_new_email(
                inbox_id=inbox["id"],
                to=to,
                subject=subject,
                text=body,
                agenda=agenda,
                origin_session_key=session_key,
                attachments=attachment_dicts,
            )
            response = {"ok": True, "thread_id": result.get("thread_id", "")}
            if attachment_dicts:
                response["attachments_sent"] = [a["filename"] for a in attachment_dicts]
            return json.dumps(response)
        except Exception as e:
            logger.warning("email_send failed: %s", e)
            return f"Error sending email: {e}"

    return [email_send]


def make_email_thread_tools(
    ctx: AppContext, *, contact_id: str | None = None, is_trusted: bool = False,
) -> list[Tool]:
    """Create email thread tools (read + search).

    Args:
        contact_id: The current session's contact. Used to scope search results
            for untrusted contacts to only their own threads.
        is_trusted: If True, search returns all threads. If False, only threads
            belonging to contact_id.
    """

    @tool
    async def email_thread_read(thread_id: str) -> str:
        """Read the full transcript of an email thread by its agentmail thread ID.
        Returns subject, participants, and all messages in chronological order.
        Use this to look up the original context behind an email-sourced memory bulletin."""
        thread = await ctx.db.fetch_one(
            "SELECT agentmail_thread_id, subject, inbox_id, contact_id, last_message_at "
            "FROM email_threads WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
            (thread_id,),
        )
        if thread is None:
            return json.dumps({"error": f"Thread not found: {thread_id}"})

        messages = await ctx.db.fetch_all(
            "SELECT sender_email, sender_name, subject, text_body, message_timestamp "
            "FROM email_messages WHERE thread_id = ? ORDER BY message_timestamp ASC",
            (thread_id,),
        )

        # Resolve inbox address to detect outbound messages
        inbox = await ctx.db.fetch_one(
            "SELECT email_address FROM email_inboxes WHERE id = ?",
            (thread["inbox_id"],),
        )
        inbox_email = (inbox["email_address"] or "").lower() if inbox else ""

        lines = []
        for msg in messages:
            text = (msg.get("text_body") or "").strip()
            if not text:
                continue
            sender_email = (msg.get("sender_email") or "").lower()
            sender_name = msg.get("sender_name") or msg.get("sender_email") or "Unknown"
            subject = msg.get("subject", "(no subject)")
            ts = msg.get("message_timestamp", "")
            role = "assistant" if sender_email == inbox_email else sender_name
            lines.append(f"[{ts}] [{role}] [Subject: {subject}]\n{text}")

        return json.dumps({
            "thread_id": thread["agentmail_thread_id"],
            "subject": thread.get("subject", ""),
            "contact_id": thread.get("contact_id"),
            "message_count": len(messages),
            "transcript": "\n\n".join(lines),
        })

    @tool
    async def email_thread_search(query: str) -> str:
        """Search email threads by keyword. Returns a ranked list of matching threads
        with thread_id, subject, contact name, message count, and last message date.
        Use email_thread_read with the thread_id to get the full transcript."""
        import re

        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return json.dumps({"error": "Empty query", "results": []})

        # Build WHERE clause for text search across subjects and message bodies
        # Each term must match in subject or any message body
        msg_conditions = " OR ".join(
            "(em.text_body LIKE ? OR em.subject LIKE ?)" for _ in terms
        )
        msg_params = []
        for t in terms:
            like = f"%{t}%"
            msg_params.extend([like, like])

        # Scope: untrusted contacts only see their own threads
        scope_clause = ""
        scope_params: list[str] = []
        if not is_trusted and contact_id:
            scope_clause = "AND et.contact_id = ?"
            scope_params.append(contact_id)

        # Find threads where the subject matches or any message matches
        sql = (
            "SELECT et.agentmail_thread_id, et.subject, et.contact_id, "
            "et.message_count, et.last_message_at, "
            "c.name as contact_name, "
            "COUNT(DISTINCT em.id) as matching_messages, "
            "CASE WHEN et.subject LIKE ? THEN 1 ELSE 0 END as subject_match "
            "FROM email_threads et "
            "LEFT JOIN email_messages em ON em.thread_id = et.agentmail_thread_id "
            f"AND ({msg_conditions}) "
            "LEFT JOIN contacts c ON c.id = et.contact_id "
            f"WHERE et.deleted_at IS NULL AND et.is_active = 1 {scope_clause} "
            "GROUP BY et.agentmail_thread_id "
            "HAVING matching_messages > 0 OR subject_match = 1 "
            "ORDER BY subject_match DESC, matching_messages DESC, et.last_message_at DESC "
            "LIMIT 20"
        )

        # Subject match term (first term for ranking)
        subject_like = f"%{terms[0]}%"

        params = [subject_like] + msg_params + scope_params

        rows = await ctx.db.fetch_all(sql, tuple(params))

        results = []
        for row in rows:
            results.append({
                "thread_id": row["agentmail_thread_id"],
                "subject": row.get("subject", ""),
                "contact_name": row.get("contact_name"),
                "message_count": row.get("message_count", 0),
                "matching_messages": row.get("matching_messages", 0),
                "last_message_at": row.get("last_message_at"),
            })

        return json.dumps({"query": query, "result_count": len(results), "results": results})

    return [email_thread_read, email_thread_search]
