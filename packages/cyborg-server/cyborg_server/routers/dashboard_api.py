"""Dashboard HTTP API — serves page data as JSON for the React SPA."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from cyborg_server.database import Database

logger = logging.getLogger(__name__)

router = APIRouter()

_STREAMABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".pdf"}


def _resolve_workspace_path(settings: Any, path: str) -> Path:
    """Resolve a relative path against the workspace dir, preventing traversal."""
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    resolved = (workspace / path) if path else workspace
    if ".." in Path(path).parts:
        raise ValueError(f"Path escapes workspace directory")
    return resolved


def _utc(val: str | None) -> str | None:
    if val and not val.endswith("Z") and not val.endswith("+00:00"):
        return val + "Z"
    return val


def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_channel(session_key: str) -> str:
    if ":voice:" in session_key or session_key.startswith("bobvoice:"):
        return "voice"
    if session_key.startswith("subagent:"):
        return "subagent"
    if ":whatsapp:" in session_key:
        return "whatsapp"
    if ":email:" in session_key:
        return "email"
    if ":phone:" in session_key:
        return "phone"
    return "other"


def _check_auth(request: Request) -> bool:
    settings = request.app.state.settings
    if not settings.dashboard_secret_configured:
        return True
    secret = request.query_params.get("secret", "")
    if not secret:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            secret = auth[7:]
    return secret == settings.dashboard_secret


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/api/home")
async def get_home(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Active sessions
    active_sessions: list[dict[str, Any]] = []
    log_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    msgs_exists_home = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if log_exists:
        rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) || 'Z' as last_activity,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                      ROUND(AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END), 2) as avg_latency
               FROM llm_call_log
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        for row in rows:
            key = row["session_key"]
            active_sessions.append({
                "session_key": key,
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": row["last_activity"],
            })
    if msgs_exists_home:
        seen = {s["session_key"] for s in active_sessions}
        msg_rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM session_messages
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        for row in msg_rows:
            key = row["session_key"]
            if key not in seen:
                active_sessions.append({
                    "session_key": key,
                    "channel": _parse_channel(key),
                    "call_count": 0,
                    "completed": 0,
                    "failed": 0,
                    "avg_latency": 0.0,
                    "last_activity": row["last_activity"],
                    "msg_count": row["msg_count"],
                })
        active_sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)

    # LLM calls chart: 24h by 15min buckets, stacked by call_category
    chart_buckets: list[dict[str, Any]] = []
    chart_categories: list[str] = []
    if log_exists:
        chart_rows = await db.fetch_all(
            """SELECT
                  strftime('%Y-%m-%dT%H:%M',
                      datetime(strftime('%s', created_at) - strftime('%s', created_at) % 900, 'unixepoch')
                  ) as interval_start,
                  call_category,
                  COUNT(*) as count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
               GROUP BY interval_start, call_category
               ORDER BY interval_start"""
        )
        bucket_map: dict[str, dict[str, int]] = {}
        categories: set[str] = set()
        for row in chart_rows:
            iv = row["interval_start"]
            cat = row["call_category"] or "other"
            categories.add(cat)
            bucket_map.setdefault(iv, {})[cat] = row["count"]
        if categories:
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            epoch = int(now.timestamp())
            start_epoch = ((epoch - 86400) // 900) * 900
            for i in range(96):
                ts = start_epoch + 900 * i
                key = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
                entry: dict[str, Any] = {"interval_start": key}
                for cat in sorted(categories):
                    entry[cat] = bucket_map.get(key, {}).get(cat, 0)
                chart_buckets.append(entry)
        chart_categories = sorted(categories)

    # Recent bulletins
    recent_bulletins: list[dict[str, Any]] = []
    bulletins_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_bulletins'"
    )
    if bulletins_table:
        b_rows = await db.fetch_all(
            """SELECT id, channel_id, source_type, content, created_at
               FROM memory_bulletins
               ORDER BY created_at DESC
               LIMIT 5"""
        )
        for row in b_rows:
            recent_bulletins.append({
                "id": row["id"],
                "channel_id": row["channel_id"],
                "source_type": row["source_type"],
                "content": row["content"],
                "created_at": _utc(row["created_at"]),
            })

    # Estimated 24h costs by call category
    cost_by_category: list[dict[str, Any]] = []
    total_cost_24h = 0.0
    if log_exists:
        cost_rows = await db.fetch_all(
            """SELECT call_category, model,
                      SUM(COALESCE(prompt_tokens, 0)) as total_prompt_tokens,
                      SUM(COALESCE(completion_tokens, 0)) as total_completion_tokens,
                      SUM(COALESCE(cached_tokens, 0)) as total_cached_tokens,
                      COUNT(*) as call_count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
               GROUP BY call_category, model
               ORDER BY call_category, model"""
        )
        # Pricing per 1M tokens (input, output)
        _PRICING: dict[str, tuple[float, float]] = {
            "gpt-5.4-mini": (1.50, 6.00),
            "gpt-5.5": (6.00, 24.00),
        }
        category_totals: dict[str, dict[str, Any]] = {}
        for row in cost_rows:
            cat = row["call_category"] or "other"
            model = row["model"] or "gpt-5.4-mini"
            prompt = row["total_prompt_tokens"] or 0
            completion = row["total_completion_tokens"] or 0
            cached = row["total_cached_tokens"] or 0
            rate_in, rate_out = _PRICING.get(model, _PRICING.get("gpt-5.4-mini", (1.50, 6.00)))
            cost = ((prompt - cached) * rate_in + cached * rate_in * 0.5 + completion * rate_out) / 1_000_000
            cost = max(cost, 0)
            if cat not in category_totals:
                category_totals[cat] = {"category": cat, "cost": 0.0, "call_count": 0, "prompt_tokens": 0, "completion_tokens": 0}
            category_totals[cat]["cost"] += cost
            category_totals[cat]["call_count"] += row["call_count"] or 0
            category_totals[cat]["prompt_tokens"] += prompt
            category_totals[cat]["completion_tokens"] += completion
        cost_by_category = sorted(category_totals.values(), key=lambda x: x["cost"], reverse=True)
        total_cost_24h = round(sum(c["cost"] for c in cost_by_category), 4)
        for c in cost_by_category:
            c["cost"] = round(c["cost"], 4)

    return {
        "active_sessions": active_sessions,
        "chart_buckets": chart_buckets,
        "chart_categories": chart_categories,
        "recent_bulletins": recent_bulletins,
        "cost_by_category": cost_by_category,
        "total_cost_24h": total_cost_24h,
    }


@router.get("/api/sessions")
async def get_sessions(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    sessions: list[dict[str, Any]] = []
    log_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    msgs_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if log_exists:
        rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) || 'Z' as last_activity,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                      ROUND(AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END), 2) as avg_latency
               FROM llm_call_log
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 100"""
        )
        for row in rows:
            key = row["session_key"]
            sessions.append({
                "session_key": key,
                "channel": _parse_channel(key),
                "call_count": row["call_count"],
                "completed": row["completed"],
                "failed": row["failed"],
                "avg_latency": row["avg_latency"] or 0.0,
                "last_activity": row["last_activity"],
            })
    # Include sessions that only have messages (no llm_call_log) — e.g. outreach targets
    if msgs_exists:
        seen = {s["session_key"] for s in sessions}
        msg_rows = await db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM session_messages
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT 100"""
        )
        for row in msg_rows:
            key = row["session_key"]
            if key not in seen:
                sessions.append({
                    "session_key": key,
                    "channel": _parse_channel(key),
                    "call_count": 0,
                    "completed": 0,
                    "failed": 0,
                    "avg_latency": 0.0,
                    "last_activity": row["last_activity"],
                    "msg_count": row["msg_count"],
                })
        sessions.sort(key=lambda s: s.get("last_activity") or "", reverse=True)
    return {"sessions": sessions}


@router.get("/api/sessions/{session_key:path}")
async def get_session_detail(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    # Resolve session context (group name, thread subject, etc.)
    session_context: dict[str, Any] = {
        "kind": None,
        "display_name": None,
        "description": None,
        "member_count": None,
        "email_participants": None,
    }
    sr_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_routes'"
    )
    if sr_table:
        route = await db.fetch_one(
            "SELECT channel, kind, chat_id, contact_id FROM session_routes "
            "WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if route:
            kind = route["kind"]
            chat_id = route["chat_id"]
            session_context["kind"] = kind

            if kind == "group" and chat_id:
                wg_table = await db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='whatsappgroups'"
                )
                if wg_table:
                    group = await db.fetch_one(
                        "SELECT name, description, member_count FROM whatsappgroups "
                        "WHERE whatsapp_jid = ? AND deleted_at IS NULL",
                        (chat_id,),
                    )
                    if group:
                        session_context["display_name"] = group["name"]
                        session_context["description"] = group["description"]
                        session_context["member_count"] = group["member_count"]

            elif kind == "thread" and chat_id:
                et_table = await db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='email_threads'"
                )
                if et_table:
                    thread = await db.fetch_one(
                        "SELECT subject FROM email_threads "
                        "WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
                        (chat_id,),
                    )
                    if thread:
                        session_context["display_name"] = thread["subject"]
                        em_table = await db.fetch_one(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_messages'"
                        )
                        if em_table:
                            email_parts = await db.fetch_all(
                                "SELECT DISTINCT sender_email, sender_name FROM email_messages em "
                                "INNER JOIN email_threads et ON et.id = em.thread_id "
                                "WHERE et.agentmail_thread_id = ? ORDER BY em.message_timestamp ASC",
                                (chat_id,),
                            )
                            session_context["email_participants"] = [
                                {"email": p["sender_email"], "name": p["sender_name"]}
                                for p in email_parts
                            ]

            elif kind == "dm":
                contact_id = route["contact_id"]
                if contact_id:
                    contact = await db.fetch_one(
                        "SELECT name FROM contacts WHERE id = ? AND deleted_at IS NULL",
                        (contact_id,),
                    )
                    if contact:
                        session_context["display_name"] = contact["name"]

    calls: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_call_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT l.id, l.created_at, l.call_category, l.status, l.latency_seconds,
                      l.ttft_seconds, l.total_tokens, l.prompt_tokens, l.completion_tokens,
                      l.messages_json, l.user_message, l.response_text,
                      l.error_message, l.contact_id, l.model,
                      c.name as contact_name
               FROM llm_call_log l
               LEFT JOIN contacts c ON c.id = l.contact_id AND c.deleted_at IS NULL
               WHERE l.session_key = ?
               ORDER BY l.created_at DESC
               LIMIT 100""",
            (session_key,),
        )
        for row in rows:
            is_reflection = row.get("call_category") == "reflection"
            tool_count = 0
            msgs_raw = row.get("messages_json")
            if msgs_raw:
                try:
                    tool_count = json.loads(msgs_raw).count('"function_call"')
                except (json.JSONDecodeError, TypeError):
                    pass
            calls.append({
                "id": row["id"],
                "created_at": _utc(row["created_at"]),
                "call_category": row.get("call_category", ""),
                "status": row["status"],
                "latency_seconds": row.get("latency_seconds"),
                "ttft_seconds": row.get("ttft_seconds"),
                "total_tokens": row.get("total_tokens"),
                "prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "tool_count": tool_count,
                "model": row.get("model", ""),
                "user_message": (row.get("user_message") or "") if is_reflection else (row.get("user_message") or "")[:300],
                "response_preview": (row.get("response_text") or "") if is_reflection else (row.get("response_text") or "")[:300],
                "error_message": row.get("error_message"),
                "contact_id": row.get("contact_id"),
                "contact_name": row.get("contact_name"),
            })

    participants: list[dict[str, Any]] = []
    participants_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_participants'"
    )
    if participants_table:
        p_rows = await db.fetch_all(
            "SELECT sp.display_name, sp.identifier, sp.contact_id, sp.is_trusted, sp.last_active_at, "
            "COALESCE(c.name, sp.display_name, sp.identifier) as resolved_name "
            "FROM session_participants sp "
            "LEFT JOIN contacts c ON c.id = sp.contact_id AND c.deleted_at IS NULL "
            "WHERE sp.session_key = ? ORDER BY sp.last_active_at DESC",
            (session_key,),
        )
        for row in p_rows:
            participants.append({
                "display_name": row["resolved_name"],
                "identifier": row["identifier"],
                "contact_id": row["contact_id"],
                "is_trusted": bool(row.get("is_trusted", 0)),
                "last_active": row["last_active_at"],
            })

    summaries: list[dict[str, Any]] = []

    agenda_row = await db.fetch_one(
        "SELECT agenda FROM session_agendas WHERE session_key = ?", (session_key,)
    )
    current_agenda = agenda_row["agenda"] if agenda_row else ""

    # Session messages (conversation entries from session_messages table)
    messages: list[dict[str, Any]] = []
    msgs_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages'"
    )
    if msgs_table:
        m_rows = await db.fetch_all(
            "SELECT sm.id, sm.role, sm.content, sm.channel, sm.sender_id, sm.created_at, "
            "COALESCE(c.name, sp.display_name) as sender_name "
            "FROM session_messages sm "
            "LEFT JOIN contacts c ON c.id = sm.sender_id AND c.deleted_at IS NULL "
            "LEFT JOIN session_participants sp ON sp.contact_id = sm.sender_id AND sp.session_key = sm.session_key "
            "WHERE sm.rowid IN ("
            "  SELECT rowid FROM session_messages"
            "  WHERE session_key = ? ORDER BY created_at DESC LIMIT 200"
            ") ORDER BY sm.created_at ASC",
            (session_key,),
        )
        for row in m_rows:
            messages.append({
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "channel": row["channel"],
                "sender_id": row["sender_id"],
                "sender_name": row.get("sender_name"),
                "created_at": _utc(row["created_at"]),
            })

    return {
        "session_key": session_key,
        "channel": _parse_channel(session_key),
        "session_context": session_context,
        "calls": calls,
        "messages": messages,
        "participants": participants,
        "summaries": summaries,
        "current_agenda": current_agenda,
        "stats": {
            "total_calls": len(calls),
            "completed": sum(1 for c in calls if c["status"] == "completed"),
            "failed": sum(1 for c in calls if c["status"] == "failed"),
        },
    }


@router.put("/api/sessions/{session_key:path}/agenda")
async def put_agenda(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    agenda = (body.get("agenda") or "").strip()
    db = _db(request)
    now = _utc_now()
    await db.execute(
        """INSERT INTO session_agendas (session_key, agenda, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(session_key) DO UPDATE SET agenda = excluded.agenda, updated_at = excluded.updated_at""",
        (session_key, agenda, now),
    )
    return {"ok": True}


@router.post("/api/sessions/{session_key:path}/reflect")
async def post_reflect(request: Request, session_key: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "query required"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.reflection_service import ReflectionService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    service = ReflectionService(ctx)
    try:
        result = await service.reflect(session_key, query)
        return result
    except Exception as exc:
        logger.error("Reflection failed for session=%s: %s", session_key, exc)
        return {"error": "reflection failed", "detail": str(exc)}


@router.get("/api/contacts")
async def get_contacts(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contacts: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
    )
    if table_exists:
        rows = await db.fetch_all(
            """SELECT c.id, c.name, c.phone_number, c.email,
                      c.is_trusted, c.is_default,
                      c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM session_participants sp WHERE sp.contact_id = c.id) as session_count,
                      (SELECT MAX(sp.last_active_at) FROM session_participants sp WHERE sp.contact_id = c.id) as last_active
               FROM contacts c
               WHERE c.deleted_at IS NULL
               ORDER BY c.name"""
        )
        for row in rows:
            contacts.append({
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row["email"],
                "is_trusted": bool(row["is_trusted"]),
                "is_default": bool(row["is_default"]),
                "session_count": row["session_count"],
                "last_active": _utc(row["last_active"]),
                "created_at": _utc(row["created_at"]),
                "updated_at": _utc(row["updated_at"]),
            })
    return {"contacts": contacts}


@router.get("/api/contacts/{contact_id}")
async def get_contact_detail(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contact = await db.fetch_one(
        """SELECT id, name, phone_number, email, metadata,
                  is_trusted, is_default, created_at, updated_at
           FROM contacts WHERE id = ? AND deleted_at IS NULL""",
        (contact_id,),
    )
    if not contact:
        return {"id": None}

    sessions: list[dict[str, Any]] = []
    participants_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_participants'"
    )
    if participants_table:
        session_rows = await db.fetch_all(
            """SELECT sp.session_key, sp.last_active_at,
                      (SELECT COUNT(*) FROM llm_call_log l WHERE l.session_key = sp.session_key) as call_count
               FROM session_participants sp
               WHERE sp.contact_id = ?
               ORDER BY sp.last_active_at DESC""",
            (contact_id,),
        )
        for row in session_rows:
            sessions.append({
                "session_key": row["session_key"],
                "channel": _parse_channel(row["session_key"]),
                "call_count": row["call_count"],
                "last_active": _utc(row["last_active_at"]),
            })

    groups: list[dict[str, Any]] = []
    groups_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='whatsappgroup_members'"
    )
    if groups_table:
        group_rows = await db.fetch_all(
            """SELECT g.name, g.whatsapp_jid, gm.is_admin, gm.joined_at
               FROM whatsappgroup_members gm
               JOIN whatsappgroups g ON g.id = gm.group_id
               WHERE gm.contact_id = ? AND gm.left_at IS NULL AND g.deleted_at IS NULL
               ORDER BY g.name""",
            (contact_id,),
        )
        for row in group_rows:
            groups.append({
                "name": row["name"],
                "jid": row["whatsapp_jid"],
                "is_admin": bool(row["is_admin"]),
                "joined_at": _utc(row["joined_at"]),
            })

    return {
        "id": contact["id"],
        "name": contact["name"],
        "phone_number": contact["phone_number"],
        "email": contact["email"],
        "is_trusted": bool(contact["is_trusted"]),
        "is_default": bool(contact["is_default"]),
        "metadata": json.loads(contact["metadata"]) if contact["metadata"] else {},
        "sessions": sessions,
        "groups": groups,
        "created_at": _utc(contact["created_at"]),
        "updated_at": _utc(contact["updated_at"]),
    }


@router.put("/api/contacts/{contact_id}")
async def update_contact(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    body = await request.json()
    updates: dict[str, Any] = {}
    if "name" in body and body["name"] is not None:
        updates["name"] = str(body["name"]).strip()
    if "phone_number" in body and body["phone_number"] is not None:
        updates["phone_number"] = str(body["phone_number"])
    if "email" in body:
        updates["email"] = body["email"]
    if "is_trusted" in body and body["is_trusted"] is not None:
        updates["is_trusted"] = 1 if body["is_trusted"] else 0

    if not updates:
        return {"ok": True, "updated": False}

    updates["updated_at"] = _utc_now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [contact_id]
    await db.execute(
        f"UPDATE contacts SET {set_clause} WHERE id = ? AND deleted_at IS NULL",
        tuple(values),
    )
    return {"ok": True, "updated": True}


@router.get("/api/contacts/{contact_id}/entity")
async def get_contact_entity(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one("SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return {"error": "contact not found"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory.service import MemoryService

    settings = request.app.state.settings
    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    claim_row = await db.fetch_one(
        "SELECT subject_id FROM memory_claims "
        "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
        (hex8,),
    )
    if claim_row:
        entity_id = claim_row["subject_id"]
    else:
        # Fallback: derive slug from contact name and look up person-{slug}
        import re
        name_row = await db.fetch_one("SELECT name FROM contacts WHERE id = ?", (contact_id,))
        if name_row and name_row["name"]:
            slug = re.sub(r"[^a-z0-9\-]", "", name_row["name"].strip().lower().replace(" ", "-"))
            entity_id = f"person-{slug}"

    if not entity_id:
        return {"error": "not found"}

    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)
    if not entity:
        return {"error": "not found"}

    # Render entity claims
    from cyborg_server.services.memory.claim_service import get_active_claims
    from cyborg_server.services.memory.claim_types import render_entity

    claims = await get_active_claims(db, entity.entity_id)
    claim_dicts = [
        {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
        for c in claims
    ]
    rendered = await render_entity(entity.entity_type, entity.display_name, claim_dicts, entity_id=entity.entity_id, db=db)

    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "status": entity.status,
        "rendered": rendered,
    }


@router.get("/api/contacts/{contact_id}/claims")
async def get_contact_claims(request: Request, contact_id: str) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one("SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return {"error": "contact not found"}

    from cyborg_server.services.memory.claim_service import get_active_claims

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    claim_row = await db.fetch_one(
        "SELECT subject_id FROM memory_claims "
        "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
        (hex8,),
    )
    if claim_row:
        entity_id = claim_row["subject_id"]
    else:
        import re
        name_row = await db.fetch_one("SELECT name FROM contacts WHERE id = ?", (contact_id,))
        if name_row and name_row["name"]:
            slug = re.sub(r"[^a-z0-9\-]", "", name_row["name"].strip().lower().replace(" ", "-"))
            entity_id = f"person-{slug}"

    if not entity_id:
        return []

    claims = await get_active_claims(db, entity_id)

    return [
        {
            "id": c.id,
            "claim_type_key": c.claim_type_key,
            "subject_id": c.subject_id,
            "object_id": c.object_id,
            "value": c.value,
            "status": c.status,
            "source_bulletins": c.source_bulletins,
            "visibility": c.visibility,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in claims
    ]


@router.get("/api/calls/{call_id}")
async def get_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    row = await db.fetch_one(
        """SELECT id, created_at, provider, model, call_category, session_key,
                  system_prompt, user_message, messages_json, tools_json,
                  response_text, latency_seconds, ttft_seconds,
                  prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                  status, error_message
           FROM llm_call_log WHERE id = ?""",
        (call_id,),
    )
    if not row:
        return {"error": "not found"}

    messages: list[dict[str, Any]] | None = None
    if row["messages_json"]:
        try:
            messages = json.loads(row["messages_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    tools: list[dict[str, Any]] | None = None
    if row["tools_json"]:
        try:
            tools = json.loads(row["tools_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": row["id"],
        "created_at": _utc(row["created_at"]),
        "provider": row["provider"],
        "model": row["model"],
        "call_category": row["call_category"],
        "session_key": row["session_key"],
        "status": row["status"],
        "latency_seconds": row["latency_seconds"],
        "ttft_seconds": row["ttft_seconds"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "cached_tokens": row["cached_tokens"],
        "messages": messages,
        "tools": tools,
        "response_text": row["response_text"],
        "user_message": row["user_message"],
        "system_prompt": row["system_prompt"],
        "error_message": row["error_message"],
    }


@router.get("/api/workspace")
async def list_workspace(request: Request, path: str = "", depth: int = 1) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    try:
        target = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}
    if not target.is_dir():
        return {"error": "not a directory"}

    workspace = settings.harness.workspace_dir.expanduser().resolve()
    entries: list[dict[str, Any]] = []

    def _walk(dir_path: Path, prefix: str, current_depth: int) -> None:
        if len(entries) >= 200:
            return
        try:
            children = sorted(dir_path.iterdir())
        except PermissionError:
            return
        for child in children:
            if len(entries) >= 200:
                break
            name = f"{prefix}{child.name}" if prefix else child.name
            entry: dict[str, Any] = {"name": name, "type": "dir" if child.is_dir() else "file"}
            if child.is_file():
                try:
                    entry["size_bytes"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(entry)
            if child.is_dir() and current_depth < depth:
                _walk(child, f"{name}/", current_depth + 1)

    prefix = f"{path}/" if path else ""
    _walk(target, prefix, 1)
    return {"entries": entries, "path": path, "root": str(workspace)}


@router.get("/api/workspace/file")
async def read_workspace_file(request: Request, path: str = "") -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    if not path:
        return {"error": "path required"}
    settings = request.app.state.settings
    try:
        resolved = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}
    if not resolved.is_file():
        return {"error": "not a file"}

    size = resolved.stat().st_size
    suffix = resolved.suffix.lower()

    # Images/PDFs: stream directly via FileResponse
    if suffix in _STREAMABLE_EXTENSIONS:
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            ".bmp": "image/bmp", ".ico": "image/x-icon", ".pdf": "application/pdf",
        }
        content_type = mime_map.get(suffix, "application/octet-stream")
        return FileResponse(resolved, media_type=content_type)

    # Text files
    data = resolved.read_bytes()
    # Count null bytes — a few may indicate encoding corruption, many means binary
    null_count = data[:8192].count(b"\x00")
    if null_count > 5:
        return {"type": "binary", "size_bytes": size, "path": path}

    return {"type": "text", "content": data.decode("utf-8", errors="replace"), "path": path, "size_bytes": size}


@router.put("/api/workspace/file")
async def write_workspace_file(request: Request, path: str = "") -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    if not path:
        return {"error": "path required"}
    settings = request.app.state.settings
    try:
        resolved = _resolve_workspace_path(settings, path)
    except ValueError:
        return {"error": "invalid path"}

    body = await request.json()
    content = body.get("content", "")
    if len(content.encode("utf-8")) > 200 * 1024:
        return {"error": "content too large (max 200KB)"}

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    logger.info("Dashboard workspace write: %s (%d bytes)", path, len(content))
    return {"ok": True}


# ── Memory ──────────────────────────────────────────────────────────────────


@router.get("/api/memory/stats")
async def get_memory_stats(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir
    db = _db(request)

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    # Build stats from database
    type_rows = await db.fetch_all(
        "SELECT entity_type, COUNT(*) AS count FROM memory_entities GROUP BY entity_type"
    )
    categories = {r["entity_type"]: r["count"] for r in type_rows}
    total_entries = sum(categories.values())

    # Recent entries
    recent_rows = await db.fetch_all(
        "SELECT e.entity_id, e.entity_type, e.display_name, e.updated_at, "
        " (SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') AS claim_count "
        "FROM memory_entities e ORDER BY e.updated_at DESC LIMIT 50"
    )
    recent = []
    for r in recent_rows:
        recent.append({
            "path": r["entity_id"],
            "wiki": "core",
            "category": r["entity_type"],
            "slug": r["entity_id"],
            "title": r["display_name"] or "",
            "summary": f"{r['claim_count']} claims",
            "modified": r["updated_at"],
        })

    # Pipeline status
    bulletins = await svc.read_bulletins(workspace, skip_digested=True)
    pending_bulletins = len(bulletins)

    last_dream = await db.fetch_one(
        "SELECT created_at FROM memory_dream_log ORDER BY created_at DESC LIMIT 1"
    )

    return {
        "stats": {
            "total_entries": total_entries,
            "wikis": {
                "core": {
                    "entries": total_entries,
                    "categories": categories,
                },
            },
        },
        "recent": recent[:50],
        "pending_bulletins": pending_bulletins,
        "last_dream": _utc(last_dream["created_at"]) if last_dream else None,
    }


@router.get("/api/memory/searches")
async def get_memory_searches(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    searches: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_search_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            "SELECT id, query, results_json, session_key, result_count, latency_seconds, created_at "
            "FROM memory_search_log ORDER BY created_at DESC LIMIT 100"
        )
        for row in rows:
            results = []
            abstract = ""
            try:
                parsed = json.loads(row["results_json"]) if row["results_json"] else {}
                if isinstance(parsed, dict):
                    results = parsed.get("results", [])
                    abstract = parsed.get("abstract", "")
                elif isinstance(parsed, list):
                    results = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            searches.append({
                "id": row["id"],
                "query": row["query"],
                "abstract": abstract,
                "results": results,
                "session_key": row["session_key"],
                "result_count": row["result_count"],
                "latency_seconds": row["latency_seconds"],
                "created_at": _utc(row["created_at"]),
            })
    return {"searches": searches}


@router.get("/api/memory/search")
async def run_memory_search(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    query = request.query_params.get("q", "").strip()
    if not query:
        return {"error": "missing query parameter 'q'"}

    db = _db(request)
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    import time
    start = time.monotonic()
    result = await svc.search_entries(workspace, query)
    latency = time.monotonic() - start

    # Log it
    from uuid import uuid4
    try:
        await db.execute(
            "INSERT INTO memory_search_log (id, query, results_json, session_key, result_count, latency_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid4()), query, json.dumps(result), None, len(result.get("results", [])), latency),
        )
    except Exception:
        pass

    result["latency_seconds"] = latency
    return result


@router.get("/api/memory/bulletins")
async def get_memory_bulletins(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)
    bulletins = await svc.read_bulletins(workspace, skip_digested=True)
    result = []
    for b in bulletins:
        result.append({
            "slug": b.id,
            "source_session": b.source_id,
            "source_type": b.source_type,
            "channel_id": b.channel_id,
            "content": b.content,
            "created_at": b.created_at.timestamp() if hasattr(b.created_at, "timestamp") else 0,
        })
    return {"bulletins": result}


@router.get("/api/memory/bulletins/{bulletin_id}")
async def get_memory_bulletin_detail(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    bulletin_id = request.path_params["bulletin_id"]

    row = await db.fetch_one(
        "SELECT id, created_at, channel_id, source_type, source_id, visibility, content, "
        "digested, session_range_start, session_range_end FROM memory_bulletins WHERE id = ?",
        (bulletin_id,),
    )
    if not row:
        return {"error": "not found"}

    # Find claims that reference this bulletin
    claim_rows = await db.fetch_all(
        "SELECT id, claim_type_key, subject_id, object_id, value, status, visibility, created_at "
        "FROM memory_claims WHERE source_bulletins LIKE ?",
        (f'%"{bulletin_id}"%',),
    )
    claims = [
        {
            "id": r["id"],
            "claim_type_key": r["claim_type_key"],
            "subject_id": r["subject_id"],
            "object_id": r["object_id"],
            "value": r["value"],
            "status": r["status"],
            "visibility": r["visibility"],
            "created_at": r["created_at"],
        }
        for r in claim_rows
    ]

    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "channel_id": row["channel_id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "visibility": row["visibility"],
        "content": row["content"],
        "digested": bool(row["digested"]),
        "session_range_start": row["session_range_start"],
        "session_range_end": row["session_range_end"],
        "claims": claims,
    }


@router.get("/api/memory/dreams")
async def get_memory_dreams(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    dreams: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_dream_log'"
    )
    if table_exists:
        rows = await db.fetch_all(
            "SELECT id, bulletins_processed, entries_created, bulletin_slugs, "
            "operations_json, raw_response, duration_seconds, status, created_at "
            "FROM memory_dream_log ORDER BY created_at DESC LIMIT 20"
        )
        for row in rows:
            operations = []
            try:
                parsed = json.loads(row["operations_json"]) if row["operations_json"] else []
                if isinstance(parsed, dict):
                    # Legacy format: just claims count
                    operations = []
                elif isinstance(parsed, list):
                    operations = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            slugs = []
            try:
                slugs = json.loads(row["bulletin_slugs"]) if row["bulletin_slugs"] else []
            except (json.JSONDecodeError, TypeError):
                pass
            claims_extracted = 0
            if isinstance(operations, list):
                claims_extracted = sum(
                    len(op["claims"]) if isinstance(op.get("claims"), list) else op.get("claims", 0)
                    for op in operations
                )
            dreams.append({
                "id": row["id"],
                "bulletins_processed": row["bulletins_processed"],
                "entries_created": row["entries_created"],
                "claims_extracted": claims_extracted,
                "bulletin_slugs": slugs,
                "operations": operations,
                "raw_response": row["raw_response"] or "",
                "duration_seconds": row["duration_seconds"],
                "status": row["status"],
                "created_at": _utc(row["created_at"]),
            })
    return {"dreams": dreams}


@router.get("/api/memory/category/{category}")
async def get_memory_category(request: Request, category: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)
    entries = await svc.browse_category(workspace, "core", category)
    for e in entries:
        e["path"] = f"memory/entities/{category}/{e['slug']}.md"
    return {"category": category, "entries": entries}


@router.get("/api/memory/entities")
async def get_memory_entities(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    entity_type = request.query_params.get("type", "").strip()

    query = (
        "SELECT e.entity_id, e.entity_type, e.display_name, e.status, e.updated_at, "
        "(SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') as claim_count "
        "FROM memory_entities e"
    )
    params: list[str] = []
    if entity_type:
        query += " WHERE e.entity_type = ?"
        params.append(entity_type)
    query += " ORDER BY e.updated_at DESC"

    rows = await db.fetch_all(query, tuple(params))

    # Build a summary per entity from key claims
    summary_keys = {
        "file": "file_path",
        "thing": "thing_type",
        "task": "task_status",
        "location": "location_type",
        "transport": "transport_type",
        "trip": "destination",
        "decision": "rationale",
        "event": "location",
        "stay": "accommodation",
    }

    entity_ids = [r["entity_id"] for r in rows]
    summaries: dict[str, str] = {}
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        claim_rows = await db.fetch_all(
            f"SELECT subject_id, claim_type_key, value, object_id FROM memory_claims "
            f"WHERE subject_id IN ({placeholders}) AND status = 'active'",
            tuple(entity_ids),
        )
        for cr in claim_rows:
            eid = cr["subject_id"]
            if eid in summaries:
                continue
            etype = next((r["entity_type"] for r in rows if r["entity_id"] == eid), "")
            key = summary_keys.get(etype, "")
            if key and cr["claim_type_key"] == key:
                summaries[eid] = cr["value"] or cr["object_id"] or ""

    entities = [
        {
            "entity_id": r["entity_id"],
            "entity_type": r["entity_type"],
            "display_name": r["display_name"] or "",
            "status": r["status"] or "active",
            "updated_at": _utc(r["updated_at"]),
            "claim_count": r["claim_count"],
            "summary": summaries.get(r["entity_id"], ""),
        }
        for r in rows
    ]
    return {"entities": entities}


@router.get("/api/memory/entities/{entity_id:path}")
async def get_memory_entity_detail(request: Request, entity_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    settings = request.app.state.settings

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory.service import MemoryService
    from cyborg_server.services.memory.claim_service import get_active_claims

    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)
    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)

    if not entity:
        return {"error": "not found"}

    claims = await get_active_claims(db, entity_id)

    from cyborg_server.services.memory.claim_types import render_entity

    claim_dicts = [
        {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
        for c in claims
    ]
    rendered = await render_entity(entity.entity_type, entity.display_name, claim_dicts, entity_id=entity.entity_id, db=db)

    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "status": entity.status,
        "rendered": rendered,
        "claims": [
            {
                "id": c.id,
                "claim_type_key": c.claim_type_key,
                "subject_id": c.subject_id,
                "object_id": c.object_id,
                "value": c.value,
                "status": c.status,
                "source_bulletins": c.source_bulletins,
                "visibility": c.visibility,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in claims
        ],
    }


@router.get("/api/memory/questions")
async def get_memory_questions(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    status_filter = request.query_params.get("status", "open").strip()
    rows = await db.fetch_all(
        "SELECT * FROM memory_questions WHERE status = ? ORDER BY created_at DESC LIMIT 100",
        (status_filter,),
    )
    questions = [
        {
            "id": r["id"],
            "entity_id": r["entity_id"],
            "question": r["question"],
            "options": json.loads(r["options"]) if r["options"] else [],
            "context": r["context"] or "",
            "status": r["status"],
            "answer": r["answer"],
            "created_at": _utc(r["created_at"]),
            "answered_at": _utc(r["answered_at"]) if r["answered_at"] else None,
        }
        for r in rows
    ]
    return {"questions": questions}


@router.post("/api/memory/questions/{question_id}/answer")
async def answer_memory_question(request: Request, question_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    body = await request.json()
    answer = body.get("answer", "").strip()
    if not answer:
        return {"error": "answer is required"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    workspace = request.app.state.settings.harness.workspace_dir
    return await svc.answer_question(workspace, question_id, answer)


@router.post("/api/memory/questions/{question_id}/dismiss")
async def dismiss_memory_question(request: Request, question_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    svc = MemoryService(ctx)
    return await svc.dismiss_question(question_id)


@router.get("/api/memory/claims")
async def get_memory_claims(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    conditions: list[str] = []
    params: list[str] = []

    claim_type = request.query_params.get("type", "").strip()
    if claim_type:
        conditions.append("claim_type_key = ?")
        params.append(claim_type)
    subject_id = request.query_params.get("subject_id", "").strip()
    if subject_id:
        conditions.append("subject_id = ?")
        params.append(subject_id)
    status = request.query_params.get("status", "").strip()
    if status:
        conditions.append("status = ?")
        params.append(status)

    query = "SELECT * FROM memory_claims"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT 200"

    rows = await db.fetch_all(query, tuple(params))
    claims = [
        {
            "id": r["id"],
            "claim_type_key": r["claim_type_key"],
            "subject_id": r["subject_id"],
            "object_id": r["object_id"],
            "value": r["value"],
            "status": r["status"],
            "source_bulletins": json.loads(r["source_bulletins"]) if r["source_bulletins"] else [],
            "visibility": r["visibility"],
            "created_at": _utc(r["created_at"]),
        }
        for r in rows
    ]
    return {"claims": claims}


@router.post("/api/memory/digested")
async def get_digested_bulletins(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    slugs: list[str] = body.get("slugs", [])
    if not slugs:
        return {"bulletins": []}

    db = _db(request)
    placeholders = ",".join("?" * len(slugs))
    rows = await db.fetch_all(
        f"SELECT id, content FROM memory_bulletins WHERE id IN ({placeholders}) AND digested = 1",
        tuple(slugs),
    )
    return {"bulletins": [{"slug": r["id"], "content": r["content"]} for r in rows]}


@router.post("/api/memory/redigest")
async def redigest_bulletin(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    # In v6, bulletins are immutable — redigest re-processes a bulletin through the dream pipeline
    body = await request.json()
    slug: str = body.get("slug", "")
    if not slug:
        return {"error": "missing slug"}

    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService

    ctx = AppContext(settings=settings, db=_db(request))
    svc = MemoryService(ctx)

    bulletin = await svc.read_bulletin(workspace, slug)
    if not bulletin:
        return {"error": f"bulletin not found: {slug}"}

    result = await svc.process_bulletin(workspace, bulletin)
    return {"ok": True, "slug": slug, "result": result}


@router.post("/api/memory/entities/merge")
async def merge_entities(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    canonical_id: str = body.get("canonical_id", "")
    loser_id: str = body.get("loser_id", "")
    if not canonical_id or not loser_id:
        return {"error": "missing canonical_id or loser_id"}

    db = _db(request)

    # Verify both entities exist
    for eid in (canonical_id, loser_id):
        row = await db.fetch_one(
            "SELECT entity_id FROM memory_entities WHERE entity_id = ? AND status = 'active'",
            (eid,),
        )
        if not row:
            return {"error": f"entity not found: {eid}"}

    from cyborg_server.services.memory.merge import _execute_merge
    result = await _execute_merge(db, canonical_id, loser_id)

    # Rebuild FTS + embedding for canonical
    settings = request.app.state.settings
    from cyborg_server.context import AppContext
    from cyborg_server.services.memory import MemoryService
    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)
    await svc._update_entity_fts(canonical_id)

    return {"ok": True, **result}


@router.post("/api/memory/backfill-people")
async def backfill_people(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    # In v6, people are populated through the seed process, not backfilled
    return {"ok": True, "message": "Use 'cyborg memory seed' to regenerate from session history"}


@router.post("/api/frontend-errors")
async def log_frontend_error(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}

    message = body.get("message", "Unknown frontend error")
    source = body.get("source", "")
    lineno = body.get("lineno", "")
    colno = body.get("colno", "")
    stack = body.get("stack", "")
    url = body.get("url", "")

    logger.warning(
        "Frontend error: %s (at %s:%s:%s, url: %s)%s",
        message, source, lineno, colno, url,
        f"\n  stack: {stack}" if stack else "",
    )
    return {"ok": True}


# ── Skills ──────────────────────────────────────────────────────────────────


@router.get("/api/skills/installed")
async def get_installed_skills(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    settings = request.app.state.settings
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return {"skills": []}

    from cyborg_server.services.skill_loader import _parse_frontmatter

    skills: list[dict[str, Any]] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        md = child / "skill.md"
        if not md.is_file():
            md = child / "SKILL.md"
        if not md.is_file():
            continue
        content = md.read_text(encoding="utf-8").strip()
        fm = _parse_frontmatter(content)
        skills.append({
            "name": child.name,
            "description": fm.get("description", ""),
            "trigger": fm.get("trigger", ""),
            "has_helper": (child / "helper.py").is_file(),
            "has_pyproject": (child / "pyproject.toml").is_file(),
        })
    return {"skills": skills}


@router.get("/api/skills/delegations")
async def get_skill_delegations(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_delegations'"
    )
    if not table_exists:
        return {"delegations": []}

    rows = await db.fetch_all(
        """SELECT id, session_key, user_story, plan, status,
                  files_created_json, result_summary, cost_usd,
                  error_message, created_at, updated_at
           FROM skill_delegations
           ORDER BY created_at DESC LIMIT 50"""
    )
    delegations: list[dict[str, Any]] = []
    for row in rows:
        files: list[str] = []
        if row["files_created_json"]:
            try:
                files = json.loads(row["files_created_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        delegations.append({
            "id": row["id"],
            "session_key": row["session_key"],
            "user_story": row["user_story"],
            "plan_preview": (row["plan"] or "")[:300],
            "status": row["status"],
            "files_created": files,
            "result_summary": row["result_summary"],
            "cost_usd": row["cost_usd"] or 0,
            "error_message": row["error_message"],
            "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
        })
    return {"delegations": delegations}


@router.get("/api/skills/delegations/{delegation_id}")
async def get_skill_delegation_detail(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one(
        """SELECT id, session_key, user_story, plan, status,
                  files_created_json, result_summary, cost_usd,
                  error_message, created_at, updated_at
           FROM skill_delegations WHERE id = ?""",
        (delegation_id,),
    )
    if not row:
        return {"error": "not found"}
    files: list[str] = []
    if row["files_created_json"]:
        try:
            files = json.loads(row["files_created_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": row["id"],
        "session_key": row["session_key"],
        "user_story": row["user_story"],
        "plan": row["plan"],
        "status": row["status"],
        "files_created": files,
        "result_summary": row["result_summary"],
        "cost_usd": row["cost_usd"] or 0,
        "error_message": row["error_message"],
        "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]),
    }


@router.post("/api/skills/delegations/{delegation_id}/implement")
async def implement_skill_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.skill_developer_service import SkillDeveloperService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SkillDeveloperService(ctx)
    try:
        result = await svc.implement_skill(delegation_id)
        return result
    except Exception as exc:
        logger.error("Skill implement failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/api/skills/delegations/{delegation_id}/reject")
async def reject_skill_delegation(request: Request, delegation_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    reason = (body.get("reason") or "").strip()

    from cyborg_server.context import AppContext
    from cyborg_server.services.skill_developer_service import SkillDeveloperService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SkillDeveloperService(ctx)
    try:
        result = await svc.reject_skill(delegation_id, reason)
        return result
    except Exception as exc:
        logger.error("Skill reject failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Subagents ─────────────────────────────────────────────────────────────────


@router.get("/api/subagents")
async def get_subagents(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    rows = await db.fetch_all(
        """SELECT id, parent_session_key, session_key, task, status,
                  result, error_message, agent_type, cost_usd,
                  created_at, updated_at
           FROM subagents
           ORDER BY created_at DESC LIMIT 50"""
    )
    subagents: list[dict[str, Any]] = []
    for row in rows:
        subagents.append({
            "id": row["id"],
            "parent_session_key": row["parent_session_key"],
            "session_key": row["session_key"],
            "task_preview": (row["task"] or "")[:200],
            "status": row["status"],
            "result_preview": (row["result"] or "")[:200],
            "error_message": row["error_message"],
            "agent_type": row["agent_type"],
            "cost_usd": row["cost_usd"] or 0,
            "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
        })
    return {"subagents": subagents}


@router.get("/api/subagents/{subagent_id}")
async def get_subagent_detail(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one(
        """SELECT id, parent_session_key, session_key, task, status,
                  result, error_message, agent_type, claude_session_id, cost_usd,
                  created_at, updated_at
           FROM subagents WHERE id = ?""",
        (subagent_id,),
    )
    if not row:
        return {"error": "not found"}
    return {
        "id": row["id"],
        "parent_session_key": row["parent_session_key"],
        "session_key": row["session_key"],
        "task": row["task"],
        "status": row["status"],
        "result": row["result"],
        "error_message": row["error_message"],
        "agent_type": row["agent_type"],
        "claude_session_id": row["claude_session_id"],
        "cost_usd": row["cost_usd"] or 0,
        "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]),
    }


@router.post("/api/subagents/{subagent_id}/message")
async def message_subagent(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "message is required"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.subagent_service import SubagentService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SubagentService(ctx)
    try:
        result = await svc.message_subagent(subagent_id, message)
        return result
    except Exception as exc:
        logger.error("Subagent message failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/api/subagents/{subagent_id}/kill")
async def kill_subagent(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from cyborg_server.context import AppContext
    from cyborg_server.services.subagent_service import SubagentService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SubagentService(ctx)
    try:
        result = await svc.kill_subagent(subagent_id)
        return result
    except Exception as exc:
        logger.error("Subagent kill failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Phone ────────────────────────────────────────────────────────────────────


@router.get("/api/phone/calls")
async def get_phone_calls(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='phone_calls'"
    )
    if not table_exists:
        return {"calls": []}
    rows = await db.fetch_all(
        """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                  pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                  pc.started_at, pc.completed_at,
                  c.id as contact_id, c.name as contact_name
           FROM phone_calls pc
           LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
           ORDER BY pc.started_at DESC
           LIMIT 50"""
    )
    calls: list[dict[str, Any]] = []
    for row in rows:
        calls.append({
            "id": row["id"],
            "call_sid": row["call_sid"],
            "phone_number": row["phone_number"],
            "direction": row["direction"],
            "status": row["status"],
            "agenda": row["agenda"],
            "exchange_count": row["exchange_count"] or 0,
            "duration_seconds": row["duration_seconds"],
            "recording_path": row["recording_path"],
            "started_at": _utc(row["started_at"]),
            "completed_at": _utc(row["completed_at"]),
            "contact_id": row["contact_id"],
            "contact_name": row["contact_name"],
        })
    return {"calls": calls}


@router.get("/api/phone/calls/{call_id}")
async def get_phone_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    call = await db.fetch_one(
        """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                  pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                  pc.started_at, pc.completed_at,
                  c.id as contact_id, c.name as contact_name
           FROM phone_calls pc
           LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
           WHERE pc.id = ? OR pc.call_sid = ?""",
        (call_id, call_id),
    )
    if not call:
        return {"error": "Call not found"}
    exchanges = await db.fetch_all(
        """SELECT exchange_index, user_transcript, assistant_transcript,
                  stt_ms, llm_total_ms, tts_first_chunk_ms, e2e_ms,
                  started_at, created_at
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call["id"],),
    )
    return {
        "call": {
            "id": call["id"],
            "call_sid": call["call_sid"],
            "phone_number": call["phone_number"],
            "direction": call["direction"],
            "status": call["status"],
            "agenda": call["agenda"],
            "exchange_count": call["exchange_count"] or 0,
            "duration_seconds": call["duration_seconds"],
            "recording_path": call["recording_path"],
            "started_at": _utc(call["started_at"]),
            "completed_at": _utc(call["completed_at"]),
            "contact_id": call["contact_id"],
            "contact_name": call["contact_name"],
        },
        "exchanges": [dict(e) for e in exchanges],
    }


@router.post("/api/phone/call")
async def dashboard_initiate_call(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    to_number = body.get("to", "").strip()
    if not to_number:
        return {"error": "Missing 'to' phone number"}
    agenda = body.get("agenda", "").strip()
    phone_settings = request.app.state.settings.phone
    if not phone_settings.enabled:
        return {"error": "Phone subsystem is not enabled"}

    from cyborg_server.routers.phone import initiate_outbound_call
    return await initiate_outbound_call(
        db=_db(request),
        settings=request.app.state.settings,
        phone_settings=phone_settings,
        to_number=to_number,
        agenda=agenda,
        app_state=request.app.state,
    )


@router.get("/api/phone/recording/{call_id}")
async def get_phone_recording(request: Request, call_id: str) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    call = await db.fetch_one(
        "SELECT recording_path FROM phone_calls WHERE id = ? OR call_sid = ?",
        (call_id, call_id),
    )
    if not call or not call["recording_path"]:
        return {"error": "No recording available"}
    rec_path = Path(call["recording_path"])
    if not rec_path.is_file():
        return {"error": "Recording file not found"}
    return FileResponse(rec_path, media_type="audio/wav")
