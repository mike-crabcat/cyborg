"""Entity reconciliation — LLM-driven consistency checking and repair.

After dream processes bulletins, this module reviews entities against
per-type rules. The LLM uses tools to look up related entities and
apply fixes (add/retract/supersede claims, create/delete entities).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from cyborg_server.services.memory.claim_types import (
    ENTITY_REF_CLAIM_KEYS,
    ENTITY_TYPE_PREFIXES,
    ENTITY_TYPE_REGISTRY,
    ENTITY_TYPES,
    render_entity,
)
from cyborg_server.services.memory.claim_service import (
    write_claim,
    supersede_claim,
    _is_valid_file_path,
)
from cyborg_server.services.memory.models import Claim
from cyborg_server.services.memory.merge import _execute_merge, _deduplicate_claims
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

_ENTITY_TYPE_PREFIXES = ENTITY_TYPE_PREFIXES

# ---------------------------------------------------------------------------
# Per-entity-type reconciliation rules (sourced from ENTITY_TYPE_REGISTRY)
# ---------------------------------------------------------------------------

_SKIP_EXPAND_PREFIXES = tuple(et.prefix for et in ENTITY_TYPE_REGISTRY.values() if et.skip_expand)

# ---------------------------------------------------------------------------
# Reconciliation tools — read and write access for the LLM
# ---------------------------------------------------------------------------


def make_reconciliation_tools(db: Any, *, on_entity_merged: Any = None) -> list[Tool]:
    """Create reconciliation tools bound to the given database connection.

    on_entity_merged: async callback(canonical_id) called after a merge,
                      used to queue reconciliation on the resulting entity.
    """

    @tool
    async def list_entities(entity_type: str) -> str:
        """List active entities of a given type. Returns entity IDs and display names.

        Use this to discover related entities (e.g. find all trips, all connections).
        """
        if entity_type not in ENTITY_TYPES:
            return f"Unknown entity type: {entity_type}. Valid types: {', '.join(ENTITY_TYPES)}"
        rows = await db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities "
            "WHERE entity_type = ? AND status = 'active'",
            (entity_type,),
        )
        if not rows:
            return f"No active {entity_type} entities found."
        lines = [f"{r['entity_id']} ({r['display_name'] or r['entity_id']})" for r in rows]
        return "\n".join(lines)

    @tool
    async def get_entity(entity_id: str) -> str:
        """Get full rendered details of an entity including all its claims.

        Returns the entity's type, display name, all claim values, and provenance.
        """
        row = await db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities "
            "WHERE entity_id = ? AND status = 'active'",
            (entity_id,),
        )
        if not row:
            return f"Entity not found: {entity_id}"

        claims = await db.fetch_all(
            "SELECT claim_type_key, object_id, value, source_bulletins FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ?",
            (entity_id,),
        )
        claim_dicts = [
            {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
            for r in claims
        ]

        rendered = await render_entity(
            row["entity_type"], row["display_name"], claim_dicts,
            entity_id=entity_id, db=db,
        )

        # Append provenance
        prov_lines: list[str] = []
        for r in claims:
            val = r["value"] or r["object_id"] or ""
            src = r["source_bulletins"] or ""
            src_label = ""
            if src:
                try:
                    bids = json.loads(src) if isinstance(src, str) else src
                    if bids:
                        src_label = f"  [source: {', '.join(bids)}]"
                    else:
                        src_label = "  [source: none — inferred]"
                except (json.JSONDecodeError, TypeError):
                    src_label = f"  [source: {src}]"
            elif r["claim_type_key"] not in ("truth",):
                src_label = "  [source: none — inferred]"
            prov_lines.append(f"  {r['claim_type_key']}: {val}{src_label}")
        if prov_lines:
            rendered += "\n\nProvenance:\n" + "\n".join(prov_lines)

        # Also show reverse references (entities that reference this one)
        reverse = await db.fetch_all(
            "SELECT c.claim_type_key, c.subject_id, e.display_name "
            "FROM memory_claims c "
            "LEFT JOIN memory_entities e ON e.entity_id = c.subject_id "
            "WHERE c.status = 'active' AND c.object_id = ?",
            (entity_id,),
        )
        if reverse:
            rendered += "\n\nReferenced by:"
            for rc in reverse:
                label = rc["display_name"] or rc["subject_id"]
                rendered += f"\n  - {label} [{rc['claim_type_key']}]"

        return rendered

    @tool
    async def add_claim(
        subject_id: str,
        claim_type_key: str,
        value: str = "",
        object_id: str = "",
    ) -> str:
        """Add a claim to an entity. Use value for scalar data (dates, text) and object_id for entity references.

        For entity-ref claims (connection, leg, member, etc.), use object_id not value.
        Only one of value/object_id should be set.
        """
        if not subject_id or not claim_type_key:
            return "Error: subject_id and claim_type_key are required."
        # Entity-ref claims should use object_id; ensure only one of value/object_id is set
        val = value if value else None
        obj = object_id if object_id else None
        if claim_type_key in ENTITY_REF_CLAIM_KEYS and val and not obj:
            obj, val = val, None
        # If both set, prefer object_id for entity-ref claims, value otherwise
        if val and obj:
            if claim_type_key in ENTITY_REF_CLAIM_KEYS:
                val = None
            else:
                obj = None
        claim = Claim(
            id=f"claim-recon-{uuid.uuid4().hex[:8]}",
            claim_type_key=claim_type_key,
            subject_id=subject_id,
            value=val,
            object_id=obj,
            status="active",
            source_bulletins=[],
            created_at=datetime.now(),
        )
        await write_claim(db, claim)
        return f"Added {claim_type_key} claim on {subject_id}" + (f" → {obj}" if obj else f" = {val}")

    @tool
    async def retract_claim(
        subject_id: str,
        claim_type_key: str,
        old_value: str = "",
    ) -> str:
        """Retract (supersede) an active claim on an entity.

        If old_value is provided, only retracts claims matching that value.
        If omitted, retracts all active claims of that type on the entity.
        """
        if not subject_id or not claim_type_key:
            return "Error: subject_id and claim_type_key are required."
        rows = await db.fetch_all(
            "SELECT id, value, object_id FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ? AND claim_type_key = ?",
            (subject_id, claim_type_key),
        )
        retracted = 0
        for row in rows:
            val = row["value"] or row["object_id"] or ""
            if old_value and val != old_value:
                continue
            await db.execute(
                "UPDATE memory_claims SET status = 'superseded', superseded_by = ? WHERE id = ?",
                (json.dumps(["reconciliation"]), row["id"]),
            )
            retracted += 1
            if not old_value:
                break
        return f"Retracted {retracted} {claim_type_key} claim(s) on {subject_id}"

    @tool
    async def supersede_claim_tool(
        subject_id: str,
        claim_type_key: str,
        old_value: str,
        new_value: str = "",
        new_object_id: str = "",
    ) -> str:
        """Replace a claim value. The old claim is superseded and a new one created.

        Provide either new_value (for scalars) or new_object_id (for entity refs).
        """
        if not subject_id or not claim_type_key or not old_value:
            return "Error: subject_id, claim_type_key, and old_value are required."
        rows = await db.fetch_all(
            "SELECT id, value, object_id FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ? AND claim_type_key = ?",
            (subject_id, claim_type_key),
        )
        for row in rows:
            val = row["value"] or row["object_id"] or ""
            if old_value and val != old_value:
                continue
            nv = new_value if new_value else None
            no = new_object_id if new_object_id else None
            if claim_type_key in ENTITY_REF_CLAIM_KEYS and nv and not no:
                no, nv = nv, None
            new_claim = Claim(
                id=f"claim-recon-{uuid.uuid4().hex[:8]}",
                claim_type_key=claim_type_key,
                subject_id=subject_id,
                value=nv,
                object_id=no,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await supersede_claim(db, row["id"], new_claim, "reconciliation")
            return f"Superseded {claim_type_key} on {subject_id}: {old_value} → {nv or no}"
        return f"No matching claim found: {claim_type_key}={old_value} on {subject_id}"

    @tool
    async def create_entity(
        entity_id: str,
        entity_type: str,
        claims_json: str = "[]",
    ) -> str:
        """Create a new entity with optional initial claims.

        claims_json should be a JSON array of objects with claim_type_key, value, and/or object_id.
        """
        if not entity_id or not entity_type:
            return "Error: entity_id and entity_type are required."
        display_name = entity_id.split("-", 1)[-1].replace("-", " ").title() if "-" in entity_id else entity_id
        await db.execute(
            "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
            "VALUES (?, ?, ?, 'active')",
            (entity_id, entity_type, display_name),
        )
        try:
            new_claims = json.loads(claims_json) if claims_json else []
        except json.JSONDecodeError:
            return f"Created entity {entity_id} but claims_json was invalid."
        for cl in new_claims:
            claim = Claim(
                id=f"claim-recon-{uuid.uuid4().hex[:8]}",
                claim_type_key=cl.get("claim_type_key", ""),
                subject_id=entity_id,
                value=cl.get("value"),
                object_id=cl.get("object_id"),
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(db, claim)
        return f"Created entity {entity_id} ({entity_type}) with {len(new_claims)} claims"

    @tool
    async def delete_entity(entity_id: str) -> str:
        """Archive an entity and supersede all its active claims."""
        if not entity_id:
            return "Error: entity_id is required."
        await db.execute(
            "UPDATE memory_entities SET status = 'archived' WHERE entity_id = ?",
            (entity_id,),
        )
        await db.execute(
            "UPDATE memory_claims SET status = 'superseded' "
            "WHERE subject_id = ? AND status = 'active'",
            (entity_id,),
        )
        return f"Archived entity {entity_id}"

    @tool
    async def merge_entities(canonical_id: str, loser_id: str) -> str:
        """Merge two duplicate entities. loser_id is absorbed into canonical_id.

        All claims, references, and bulletins from loser are rewritten to point
        to canonical. The loser entity is deleted. Use when two entities represent
        the same real-world thing (e.g. duplicate connections with slightly different names).
        """
        if not canonical_id or not loser_id:
            return "Error: canonical_id and loser_id are required."
        if canonical_id == loser_id:
            return "Error: canonical_id and loser_id must be different."
        # Verify both entities exist
        for eid, label in [(canonical_id, "canonical"), (loser_id, "loser")]:
            row = await db.fetch_one(
                "SELECT entity_id FROM memory_entities WHERE entity_id = ? AND status = 'active'",
                (eid,),
            )
            if not row:
                return f"Error: {label} entity {eid} not found or not active."
        result = await _execute_merge(db, canonical_id, loser_id)
        deduped = await _deduplicate_claims(db, canonical_id)
        # Queue reconciliation on the merged entity
        if on_entity_merged:
            await on_entity_merged(canonical_id)
        return (
            f"Merged {loser_id} into {canonical_id}. "
            f"{result['claims_rewritten']} claims rewritten, "
            f"{deduped} duplicates removed."
        )

    return [
        list_entities,
        get_entity,
        add_claim,
        retract_claim,
        supersede_claim_tool,
        create_entity,
        delete_entity,
        merge_entities,
    ]


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

RECONCILIATION_PROMPT = """\
You are a Memory Reconciliation Agent. You review an entity and its related \
sub-entities, checking for inconsistencies against a set of rules.

You have tools to look up other entities and apply fixes. Use them freely — \
prefer acting over asking.

## Entity Under Review

{entity_view}

## Source Bulletins (ground truth reference material)

{bulletins}

## Answered Questions (ground truth from the user)

{answers}

## Reconciliation Rules for {entity_type}

{rules}

## Your Task

1. Check the entity data against each rule above.
2. Use the Source Bulletins and claim provenance to resolve conflicts — claims with \
no source bulletin are inferred and less reliable than claims grounded in bulletins.
3. If you can determine a clear fix, use the tools to apply it — prefer acting over asking.
4. Answered questions are ground truth from the user — act on them directly, do not re-ask.
5. If a child entity has been split or merged, update the parent's composition claims (e.g. trip.leg) accordingly.
6. When splitting a stay into multiple new stays, create the new entities first, then \
retract the parent's leg claim pointing to the original and delete the original entity.
7. If the fix is truly ambiguous with no answered question guiding it, raise a question instead.
8. Use list_entities and get_entity to discover and inspect related entities when needed.
9. If the entity is consistent and no fixes are needed, respond with an empty issues list.

## Output Format

When you are done applying fixes (or if no fixes were needed), respond with a JSON object:

```json
{{
  "issues": [
    {{"rule": "which rule was violated", "detail": "what was wrong and what you did"}}
  ],
  "questions": [
    {{
      "entity_id": "entity-id-in-question",
      "question": "What should X be?",
      "options": ["Option A", "Option B"],
      "context": "Two stays overlap but may be intentional"
    }}
  ]
}}
```

Apply all fixes using tools FIRST, then return the JSON summary.
If no issues found: {{"issues": [], "questions": []}}
"""


# ---------------------------------------------------------------------------
# render_entity_full — recursive entity view assembly
# ---------------------------------------------------------------------------

async def render_entity_full(
    db: Any,
    entity_id: str,
    depth: int = 2,
    _visited: set[str] | None = None,
) -> str:
    """Render an entity with its related sub-entities expanded recursively."""
    if _visited is None:
        _visited = set()
    if entity_id in _visited:
        return f"[Cycle: {entity_id}]"
    _visited = _visited | {entity_id}

    entity_row = await db.fetch_one(
        "SELECT entity_id, entity_type, display_name FROM memory_entities "
        "WHERE entity_id = ? AND status = 'active'",
        (entity_id,),
    )
    if not entity_row:
        return f"[Unknown entity: {entity_id}]"

    claims = await db.fetch_all(
        "SELECT claim_type_key, object_id, value, source_bulletins FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (entity_id,),
    )
    claim_dicts = [
        {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
        for r in claims
    ]

    rendered = await render_entity(
        entity_row["entity_type"],
        entity_row["display_name"],
        claim_dicts,
        entity_id=entity_id,
        db=db,
    )

    # Append provenance tags (source bulletins) to each claim line
    provenance_lines: list[str] = []
    for r in claims:
        val = r["value"] or r["object_id"] or ""
        src = r["source_bulletins"] or ""
        src_label = ""
        if src:
            try:
                bids = json.loads(src) if isinstance(src, str) else src
                if bids:
                    src_label = f"  [source: {', '.join(bids)}]"
                else:
                    src_label = "  [source: none — inferred]"
            except (json.JSONDecodeError, TypeError):
                src_label = f"  [source: {src}]"
        elif r["claim_type_key"] not in ("truth",):
            src_label = "  [source: none — inferred]"
        provenance_lines.append(f"  {r['claim_type_key']}: {val}{src_label}")
    provenance_block = "Claim provenance for {}:\n".format(entity_id) + "\n".join(provenance_lines)
    rendered += "\n\n" + provenance_block

    # Skip expanding into types marked skip_expand — noise for reconciliation
    skip_prefixes = _SKIP_EXPAND_PREFIXES

    if depth > 0:
        seen_refs: set[str] = set()
        for claim in claim_dicts:
            key = claim["claim_type_key"]
            if key not in ENTITY_REF_CLAIM_KEYS:
                continue
            ref_id = claim.get("object_id") or claim.get("value") or ""
            if not ref_id or not ref_id.startswith(_ENTITY_TYPE_PREFIXES):
                continue
            if ref_id.startswith(skip_prefixes):
                continue
            if ref_id in seen_refs:
                continue
            seen_refs.add(ref_id)
            child = await render_entity_full(db, ref_id, depth - 1, _visited)
            rendered += f"\n\n--- {key}: {ref_id} ---\n{child}"

    return rendered



# ---------------------------------------------------------------------------
# reconcile_entity — LLM-driven detect + fix + question (tool-based)
# ---------------------------------------------------------------------------

async def _load_answers(db: Any, entity_id: str) -> str:
    """Load answered questions for an entity as ground-truth context."""
    rows = await db.fetch_all(
        "SELECT question, answer FROM memory_questions "
        "WHERE entity_id = ? AND status = 'answered' AND answer IS NOT NULL",
        (entity_id,),
    )
    if not rows:
        return "(none)"
    lines = [f"Q: {r['question']}\nA: {r['answer']}" for r in rows]
    return "\n".join(lines)


async def _write_questions(
    db: Any, entity_id: str, questions: list[dict],
) -> list[str]:
    """Write unresolved questions to the memory_questions table."""
    ids = []
    now = datetime.now().isoformat()
    for q in questions:
        qid = f"question-{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO memory_questions "
            "(id, entity_id, question, options, context, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (
                qid,
                entity_id,  # Always use the reconciliation root, not a leaf
                q.get("question", ""),
                json.dumps(q.get("options", [])),
                q.get("context", ""),
                now,
            ),
        )
        ids.append(qid)
        logger.info("Reconciliation question raised: %s", qid)
    return ids


async def _collect_bulletin_text(db: Any, entity_id: str) -> str:
    """Collect source bulletin text for all claims on an entity and its children.

    Gathers bulletin IDs from claim source_bulletins, loads the bulletin content,
    and returns a formatted block for the reconciliation prompt.
    """
    # Collect from direct claims
    claim_rows = await db.fetch_all(
        "SELECT source_bulletins FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ? AND source_bulletins IS NOT NULL",
        (entity_id,),
    )
    bulletin_ids: set[str] = set()
    for r in claim_rows:
        try:
            bids = json.loads(r["source_bulletins"]) if r["source_bulletins"] else []
            bulletin_ids.update(bids)
        except (json.JSONDecodeError, TypeError):
            pass

    # Also from direct entity-bulletin links
    eb_rows = await db.fetch_all(
        "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
        (entity_id,),
    )
    bulletin_ids.update(r["bulletin_id"] for r in eb_rows)

    if not bulletin_ids:
        return "(no source bulletins available)"

    # Load bulletin content
    placeholders = ",".join("?" for _ in bulletin_ids)
    rows = await db.fetch_all(
        f"SELECT id, content FROM memory_bulletins WHERE id IN ({placeholders})",
        tuple(bulletin_ids),
    )
    if not rows:
        return "(no source bulletins available)"

    lines = []
    for r in rows:
        content = r["content"] or ""
        # Truncate long bulletins to avoid bloating the prompt
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{r['id']}] {content}")
    return "\n\n".join(lines)


async def deprecate_file_entities_without_path(db: Any) -> list[str]:
    """Find active file entities with no valid file_path claim and deprecate them.

    Returns list of deprecated entity IDs.
    """
    file_entities = await db.fetch_all(
        "SELECT entity_id FROM memory_entities "
        "WHERE entity_type = 'file' AND status = 'active'"
    )
    if not file_entities:
        return []

    deprecated: list[str] = []
    for row in file_entities:
        eid = row["entity_id"]
        path_rows = await db.fetch_all(
            "SELECT value FROM memory_claims "
            "WHERE subject_id = ? AND claim_type_key = 'file_path' AND status = 'active'",
            (eid,),
        )
        has_valid_path = any(
            r["value"] and _is_valid_file_path(r["value"])
            for r in path_rows
        )
        if not has_valid_path:
            await db.execute(
                "UPDATE memory_entities SET status = 'deprecated' WHERE entity_id = ?",
                (eid,),
            )
            await db.execute(
                "UPDATE memory_claims SET status = 'superseded' "
                "WHERE subject_id = ? AND status = 'active'",
                (eid,),
            )
            deprecated.append(eid)
            logger.info("Deprecated file entity (no valid file_path): %s", eid)

    return deprecated


async def reconcile_entity(
    db: Any,
    llm: Any,
    entity_id: str,
    *,
    update_fts_fn=None,
    schedule_reconciliation_fn=None,
) -> dict[str, Any]:
    """Run reconciliation for a single entity using tool-based LLM interaction.

    The LLM has access to tools for looking up related entities and applying
    fixes directly. Returns {"issues": [...], "operations_applied": [...], "questions_raised": [...]}.

    schedule_reconciliation_fn: async callback(entity_ids) to queue post-merge
                                 reconciliation on the resulting entities.
    """
    entity_row = await db.fetch_one(
        "SELECT entity_type FROM memory_entities WHERE entity_id = ? AND status = 'active'",
        (entity_id,),
    )
    if not entity_row:
        return {"issues": [], "operations_applied": [], "questions_raised": []}

    entity_type = entity_row["entity_type"]
    et_def = ENTITY_TYPE_REGISTRY.get(entity_type)
    rules = et_def.reconciliation_rules if et_def else "No specific reconciliation rules."

    entity_view = await render_entity_full(db, entity_id)

    answers = await _load_answers(db, entity_id)

    # Collect source bulletin text for provenance
    bulletin_text = await _collect_bulletin_text(db, entity_id)

    system_prompt = RECONCILIATION_PROMPT.format(
        entity_view=entity_view,
        bulletins=bulletin_text,
        answers=answers,
        entity_type=entity_type,
        rules=rules,
    )

    # Build tools for the reconciliation LLM
    async def _on_entity_merged(canonical_id: str) -> None:
        """Queue reconciliation on the merged entity."""
        if schedule_reconciliation_fn:
            await schedule_reconciliation_fn([canonical_id])

    recon_tools = make_reconciliation_tools(db, on_entity_merged=_on_entity_merged)

    # Track entities touched by tool calls for FTS updates
    touched_entities: set[str] = {entity_id}

    # Wrap tool handlers to track touched entities
    original_handlers = {t.name: t.handler for t in recon_tools}

    def _track(entity_ids: set[str]):
        """Return a decorator that tracks entity IDs touched by tool calls."""
        def _wrap_handler(handler):
            async def _tracked(*args, **kwargs):
                result = await handler(*args, **kwargs)
                # Track subject_id if passed
                sid = kwargs.get("subject_id", "")
                if sid:
                    entity_ids.add(sid)
                eid = kwargs.get("entity_id", "")
                if eid:
                    entity_ids.add(eid)
                return result
            return _tracked
        return _wrap_handler

    for t in recon_tools:
        t.handler = _track(touched_entities)(original_handlers[t.name])

    response = await llm.chat_with_tools(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Review this entity for consistency issues."},
        ],
        tools=recon_tools,
        call_category="memory_reconciliation",
    )

    # Parse the final JSON response for issues and questions
    issues: list[dict] = []
    questions: list[dict] = []
    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        issues = result.get("issues", [])
        questions = result.get("questions", [])
    except (json.JSONDecodeError, ValueError):
        # Tools already applied their effects — just log the parse failure
        logger.warning("Reconciliation: failed to parse final response for %s", entity_id)

    question_ids = await _write_questions(db, entity_id, questions)

    # Re-render FTS for all affected entities
    if update_fts_fn:
        for eid in touched_entities:
            try:
                await update_fts_fn(eid)
            except Exception:
                pass

    if issues or question_ids:
        logger.info(
            "Reconciliation for %s: %d issues, %d questions",
            entity_id, len(issues), len(question_ids),
        )

    return {
        "issues": issues,
        "operations_applied": [],  # Operations were applied via tools, not batched
        "questions_raised": question_ids,
    }
