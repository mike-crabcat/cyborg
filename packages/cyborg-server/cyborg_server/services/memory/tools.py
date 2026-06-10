"""Memory tools — recall, find, note.

Three minimal tools for the memory system:
- recall(query) — retrieve entity + claims by ID, name, or natural language
- find(entity_type, claim_type_key?, value?) — structured search across claims
- note(text, context?) — accept new information, queue as bulletin
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cyborg_server.services.memory.claim_types import render_entity
from cyborg_server.services.memory.models import ENTITY_TYPES

logger = logging.getLogger(__name__)


async def recall(
    db: Any,
    query: str,
    actor: str | None = None,
) -> str:
    """Retrieve entity information by ID, name, embedding similarity, or FTS query.

    Tries exact ID, alias, embedding search (top 3), then FTS.
    For natural language queries, embedding search returns multiple results.
    """
    # Try direct entity ID lookup
    entity = await _resolve_entity(db, query)
    if not entity:
        return f"No entity found matching: {query}"

    entity_id = entity["entity_id"]
    entity_type = entity["entity_type"]
    display_name = entity["display_name"]

    # Fetch active claims
    claims = await db.fetch_all(
        "SELECT claim_type_key, object_id, value FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (entity_id,),
    )

    claim_dicts = [
        {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
        for r in claims
    ]

    # Also fetch claims where this entity is the object (reverse lookups)
    reverse_claims = await db.fetch_all(
        "SELECT c.claim_type_key, c.subject_id, e.display_name "
        "FROM memory_claims c "
        "LEFT JOIN memory_entities e ON e.entity_id = c.subject_id "
        "WHERE c.status = 'active' AND c.object_id = ?",
        (entity_id,),
    )

    rendered = await render_entity(entity_type, display_name, claim_dicts, entity_id=entity_id, db=db)

    # Append reverse references
    if reverse_claims:
        rendered += "\n\nReferenced by:"
        for rc in reverse_claims:
            label = rc["display_name"] or rc["subject_id"]
            rendered += f"\n  - {label} [{rc['claim_type_key']}]"

    # If embedding search returned multiple results, append the others
    extra_ids = entity.get("_extra_ids", [])
    if extra_ids:
        for eid in extra_ids:
            e_row = await db.fetch_one(
                "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
                (eid,),
            )
            if not e_row:
                continue
            e_claims = await db.fetch_all(
                "SELECT claim_type_key, object_id, value FROM memory_claims "
                "WHERE status = 'active' AND subject_id = ?",
                (eid,),
            )
            e_dicts = [
                {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
                for r in e_claims
            ]
            e_rendered = await render_entity(e_row["entity_type"], e_row["display_name"], e_dicts, entity_id=eid, db=db)
            rendered += f"\n\n---\n{e_rendered}"

    return rendered


async def find(
    db: Any,
    entity_type: str,
    claim_type_key: str | None = None,
    value: str | None = None,
) -> str:
    """Structured search across entities by type and optional claim filters."""
    if entity_type not in ENTITY_TYPES:
        return f"Unknown entity type: {entity_type}. Valid types: {', '.join(ENTITY_TYPES)}"

    if claim_type_key and value:
        # Search by claim type + value
        rows = await db.fetch_all(
            "SELECT DISTINCT e.entity_id, e.display_name "
            "FROM memory_entities e "
            "JOIN memory_claims c ON c.subject_id = e.entity_id "
            "WHERE e.entity_type = ? AND c.claim_type_key = ? "
            "AND c.value LIKE ? AND c.status = 'active'",
            (entity_type, claim_type_key, f"%{value}%"),
        )
    elif claim_type_key:
        # Search by claim type only
        rows = await db.fetch_all(
            "SELECT DISTINCT e.entity_id, e.display_name "
            "FROM memory_entities e "
            "JOIN memory_claims c ON c.subject_id = e.entity_id "
            "WHERE e.entity_type = ? AND c.claim_type_key = ? "
            "AND c.status = 'active'",
            (entity_type, claim_type_key),
        )
    else:
        # List all entities of this type
        rows = await db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities "
            "WHERE entity_type = ? AND status = 'active'",
            (entity_type,),
        )

    if not rows:
        return f"No {entity_type} entities found."

    lines = [f"{entity_type.title()} entities:"]
    for r in rows:
        lines.append(f"  - {r['display_name'] or r['entity_id']}")
    return "\n".join(lines)


async def note(
    db: Any,
    text: str,
    context_entity_id: str | None = None,
    channel_id: str = "manual",
    source_type: str = "note",
    visibility: str = "channel",
) -> str:
    """Accept new information from conversation. Queues as a bulletin for digestion."""
    bulletin_id = f"bulletin-note-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    await db.execute(
        "INSERT OR IGNORE INTO memory_bulletins "
        "(id, created_at, channel_id, source_type, source_id, visibility, content, digested) "
        "VALUES (?, ?, ?, ?, '', ?, ?, 0)",
        (
            bulletin_id,
            datetime.now(timezone.utc).isoformat(),
            channel_id,
            source_type,
            visibility,
            text,
        ),
    )

    # Link to context entity if provided
    if context_entity_id:
        await db.execute(
            "INSERT OR IGNORE INTO memory_bulletin_entities "
            "(bulletin_id, category, entity_id, resolution_status) "
            "VALUES (?, 'context', ?, 'known')",
            (bulletin_id, context_entity_id),
        )

    logger.info("Note queued as bulletin: %s", bulletin_id)
    return f"Noted: {bulletin_id}"


async def _resolve_entity(db: Any, query: str) -> dict | None:
    """Resolve a query to an entity row.

    Tries: exact entity_id match, then alias match, then embedding
    similarity search, then FTS keyword search.
    """
    # Exact entity ID
    row = await db.fetch_one(
        "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
        (query,),
    )
    if row:
        return dict(row)

    # Alias lookup (case-insensitive)
    alias_row = await db.fetch_one(
        "SELECT entity_id FROM memory_aliases WHERE alias = ? COLLATE NOCASE",
        (query,),
    )
    if alias_row:
        row = await db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
            (alias_row["entity_id"],),
        )
        if row:
            return dict(row)

    # Embedding similarity search
    try:
        from cyborg_server.services.memory.embedding import search_similar
        results = await search_similar(db, query, limit=5, threshold=1.2)
        if results:
            top = results[0]
            row = await db.fetch_one(
                "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
                (top["entity_id"],),
            )
            if row:
                result = dict(row)
                # Attach extra matches for recall() to render
                result["_extra_ids"] = [r["entity_id"] for r in results[1:]]
                return result
    except Exception:
        pass  # Embedding search failure is non-critical

    # FTS search — split into tokens and AND them for broad matching
    tokens = query.strip().split()
    safe_tokens = []
    for t in tokens:
        escaped = t.replace('"', '""')
        safe_tokens.append(f'"{escaped}"')
    fts_query = " AND ".join(safe_tokens)
    fts_rows = await db.fetch_all(
        "SELECT entity_id FROM memory_entities_fts WHERE memory_entities_fts MATCH ? LIMIT 5",
        (fts_query,),
    )
    if fts_rows:
        row = await db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
            (fts_rows[0]["entity_id"],),
        )
        if row:
            return dict(row)

    return None
