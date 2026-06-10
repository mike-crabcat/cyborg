"""Cross-entity merge — detect and merge duplicate entities using embeddings + LLM."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from cyborg_server.services.memory.claim_types import render_entity
from cyborg_server.services.memory.cleanup import (
    rewrite_bulletin_entities,
    rewrite_claims,
    rewrite_entity_relations,
)
from cyborg_server.services.memory.embedding import delete_embedding, _unpack_embedding

logger = logging.getLogger(__name__)

# Types to skip — groups are rarely duplicated, files have unique paths,
# decisions are too abstract to compare meaningfully.
_SKIP_TYPES: frozenset[str] = frozenset({"group", "file", "decision", "task", "thing", "transport"})

# Cosine distance threshold for candidate pairs. Lower = more similar.
# 0.4 is conservative — only catches near-duplicates.
_DISTANCE_THRESHOLD: float = 0.65

MERGE_CONFIRMATION_PROMPT = """\
You are a duplicate entity detector. Given two {entity_type} entities, determine \
if they represent the SAME real-world thing.

Focus on SHARED content: overlapping members, stops, dates, locations, or claims. \
One entity may have MORE detail than the other — that does not mean they are different. \
A trip with 5 stops and a trip with 3 stops where 2+ stops overlap are likely the same trip. \
A person "Adam" and "Adam Prior" are likely the same person.
{overlap_section}
Entity A ({id_a}):
{render_a}

Entity B ({id_b}):
{render_b}

Are these the same real-world {entity_type}? Answer ONLY "YES" or "NO".
"""


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Compute cosine distance between two vectors. 0 = identical, 2 = opposite."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 2.0
    return 1.0 - dot / (norm_a * norm_b)


async def _find_candidates(
    db: Any,
    entity_type: str,
    threshold: float = _DISTANCE_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """Find candidate duplicate pairs within an entity type using embeddings."""
    rows = await db.fetch_all(
        "SELECT e.entity_id, em.embedding "
        "FROM memory_entities e "
        "JOIN memory_entity_embeddings em ON em.entity_id = e.entity_id "
        "WHERE e.entity_type = ? AND e.status = 'active'",
        (entity_type,),
    )
    if len(rows) < 2:
        return []

    # Unpack embeddings
    entities: list[tuple[str, list[float]]] = []
    for r in rows:
        emb_bytes = r["embedding"]
        if not emb_bytes:
            continue
        vec = _unpack_embedding(emb_bytes)
        entities.append((r["entity_id"], vec))

    # Pairwise comparison
    candidates: list[tuple[str, str, float]] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            id_a, vec_a = entities[i]
            id_b, vec_b = entities[j]
            dist = _cosine_distance(vec_a, vec_b)
            if dist < threshold:
                candidates.append((id_a, id_b, dist))

    candidates.sort(key=lambda t: t[2])
    return candidates


async def _confirm_merge(
    llm: Any,
    entity_type: str,
    id_a: str,
    render_a: str,
    id_b: str,
    render_b: str,
    *,
    overlap_summary: str = "",
) -> bool:
    """Use LLM to confirm that two entities are duplicates."""
    overlap_section = f"\nShared data:\n{overlap_summary}\n" if overlap_summary else ""
    prompt = MERGE_CONFIRMATION_PROMPT.format(
        entity_type=entity_type,
        id_a=id_a,
        render_a=render_a[:2000],
        id_b=id_b,
        render_b=render_b[:2000],
        overlap_section=overlap_section,
    )
    response = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
        model=llm.memory_model,
        call_category="memory_merge_confirmation",
        temperature=0.0,
        max_tokens=16,
    )
    return response.strip().upper().startswith("YES")


async def _render_entity_claims(db: Any, entity_id: str) -> str:
    """Render an entity's claims for comparison."""
    entity_row = await db.fetch_one(
        "SELECT entity_type, display_name FROM memory_entities WHERE entity_id = ?",
        (entity_id,),
    )
    if not entity_row:
        return f"[Unknown: {entity_id}]"

    claims = await db.fetch_all(
        "SELECT claim_type_key, object_id, value FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (entity_id,),
    )
    claim_dicts = [
        {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
        for r in claims
    ]
    return await render_entity(entity_row["entity_type"], entity_row["display_name"], claim_dicts, entity_id=entity_id, db=db)


async def _compute_overlap(db: Any, id_a: str, id_b: str) -> str:
    """Compute overlapping claims between two entities for the confirmation prompt."""
    claims_a = await db.fetch_all(
        "SELECT claim_type_key, value, object_id FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (id_a,),
    )
    claims_b = await db.fetch_all(
        "SELECT claim_type_key, value, object_id FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (id_b,),
    )

    # Find shared object_ids (entity references like members, stops)
    objs_a = {r["object_id"] for r in claims_a if r["object_id"]}
    objs_b = {r["object_id"] for r in claims_b if r["object_id"]}
    shared_objs = objs_a & objs_b

    # Find shared values (scalars)
    vals_a = {(r["claim_type_key"], r["value"]) for r in claims_a if r["value"]}
    vals_b = {(r["claim_type_key"], r["value"]) for r in claims_b if r["value"]}
    shared_vals = vals_a & vals_b

    lines: list[str] = []
    if shared_objs:
        lines.append(f"Shared references ({len(shared_objs)}): " + ", ".join(sorted(shared_objs)))
    if shared_vals:
        lines.append(f"Shared values ({len(shared_vals)}): " + ", ".join(
            f"{ct}={v}" for ct, v in sorted(shared_vals)
        ))
    return "\n".join(lines)


async def _pick_canonical(db: Any, id_a: str, id_b: str) -> tuple[str, str]:
    """Pick the canonical entity ID. Prefer the one with more claim references."""
    counts: dict[str, int] = {}
    for eid in (id_a, id_b):
        row = await db.fetch_one(
            "SELECT COUNT(*) as cnt FROM memory_claims "
            "WHERE status = 'active' AND (subject_id = ? OR object_id = ?)",
            (eid, eid),
        )
        counts[eid] = row["cnt"] if row else 0

    if counts[id_a] >= counts[id_b]:
        return id_a, id_b
    return id_b, id_a


async def _deduplicate_claims(db: Any, canonical_id: str) -> int:
    """Deduplicate claims that became identical after merging."""
    rows = await db.fetch_all(
        "SELECT id, claim_type_key, value, object_id FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (canonical_id,),
    )

    # Group by (claim_type_key, value, object_id)
    seen: dict[tuple[str, str, str], str] = {}
    deduped = 0
    for r in rows:
        key = (r["claim_type_key"], r["value"] or "", r["object_id"] or "")
        if key in seen:
            # Supersede the duplicate
            await db.execute(
                "UPDATE memory_claims SET status = 'superseded', superseded_by = ? WHERE id = ?",
                (json.dumps(["merge-dedup"]), r["id"]),
            )
            deduped += 1
        else:
            seen[key] = r["id"]
    return deduped


async def _execute_merge(db: Any, canonical_id: str, loser_id: str) -> dict[str, Any]:
    """Merge loser into canonical. Rewrite references, deduplicate, archive."""
    rename = {loser_id: canonical_id}

    # Rewrite all references
    claims_rewritten = await rewrite_claims(db, rename)
    bulletins_rewritten = await rewrite_bulletin_entities(db, rename)
    relations_rewritten = await rewrite_entity_relations(db, rename)

    # Also rewrite object_id references in source_bulletins JSON
    # (claims that reference loser_id as object)
    await db.execute(
        "UPDATE memory_claims SET object_id = ? WHERE object_id = ?",
        (canonical_id, loser_id),
    )

    # Rewrite aliases from loser to canonical
    await db.execute(
        "UPDATE memory_aliases SET entity_id = ? WHERE entity_id = ?",
        (canonical_id, loser_id),
    )

    # Remove self-referential claims created by the merge
    self_refs = await db.execute(
        "UPDATE memory_claims SET status = 'superseded', superseded_by = ? "
        "WHERE subject_id = ? AND object_id = ? AND status = 'active'",
        (json.dumps(["merge-self-ref"]), canonical_id, canonical_id),
    )

    # Deduplicate claims on canonical
    deduped = await _deduplicate_claims(db, canonical_id)

    # Delete loser entity and its embedding
    await db.execute("DELETE FROM memory_entities WHERE entity_id = ?", (loser_id,))
    await delete_embedding(db, loser_id)

    return {
        "canonical": canonical_id,
        "loser": loser_id,
        "claims_rewritten": claims_rewritten,
        "bulletins_rewritten": bulletins_rewritten,
        "relations_rewritten": relations_rewritten,
        "claims_deduplicated": deduped,
        "self_refs_removed": self_refs,
    }


async def run_merge(
    db: Any,
    llm: Any,
    *,
    dry_run: bool = False,
    threshold: float = _DISTANCE_THRESHOLD,
) -> dict[str, Any]:
    """Detect and merge duplicate entities across all types.

    Returns summary of merges performed.
    """
    # Load all active entity types
    type_rows = await db.fetch_all(
        "SELECT DISTINCT entity_type FROM memory_entities WHERE status = 'active'"
    )
    entity_types = [r["entity_type"] for r in type_rows if r["entity_type"] not in _SKIP_TYPES]

    all_candidates: list[tuple[str, str, float, str]] = []
    confirmed_merges: list[tuple[str, str, str]] = []

    # Phase 1: Find candidates
    for etype in entity_types:
        candidates = await _find_candidates(db, etype, threshold)
        for id_a, id_b, dist in candidates:
            all_candidates.append((id_a, id_b, dist, etype))

    if not all_candidates:
        return {"candidates": 0, "confirmed": 0, "merges": []}

    logger.info("Merge: found %d candidate pair(s)", len(all_candidates))

    # Pre-filter: load display names for name overlap check
    name_rows = await db.fetch_all(
        "SELECT entity_id, display_name FROM memory_entities WHERE status = 'active'"
    )
    names: dict[str, str] = {r["entity_id"]: r["display_name"] for r in name_rows}

    # Phase 2: LLM confirmation
    already_merged: set[str] = set()
    for id_a, id_b, dist, etype in all_candidates:
        if id_a in already_merged or id_b in already_merged:
            continue

        # Name overlap filter: skip person pairs with no shared name words
        if etype == "person":
            words_a = set(names.get(id_a, "").lower().split())
            words_b = set(names.get(id_b, "").lower().split())
            if words_a and words_b and not (words_a & words_b):
                continue

        render_a = await _render_entity_claims(db, id_a)
        render_b = await _render_entity_claims(db, id_b)
        overlap = await _compute_overlap(db, id_a, id_b)

        if await _confirm_merge(llm, etype, id_a, render_a, id_b, render_b, overlap_summary=overlap):
            canonical, loser = await _pick_canonical(db, id_a, id_b)
            confirmed_merges.append((canonical, loser, etype))
            already_merged.add(loser)
            logger.info(
                "Merge confirmed: %s -> %s (%s, distance=%.3f)",
                loser, canonical, etype, dist,
            )

    if not confirmed_merges:
        return {"candidates": len(all_candidates), "confirmed": 0, "merges": []}

    if dry_run:
        return {
            "candidates": len(all_candidates),
            "confirmed": len(confirmed_merges),
            "dry_run": True,
            "merges": [
                {"canonical": c, "loser": l, "type": t}
                for c, l, t in confirmed_merges
            ],
        }

    # Phase 3: Execute merges
    merge_results = []
    for canonical, loser, etype in confirmed_merges:
        result = await _execute_merge(db, canonical, loser)
        result["type"] = etype
        merge_results.append(result)
        logger.info(
            "Merged %s -> %s (%d claims rewritten, %d deduplicated)",
            loser, canonical, result["claims_rewritten"], result["claims_deduplicated"],
        )

    return {
        "candidates": len(all_candidates),
        "confirmed": len(confirmed_merges),
        "merges": merge_results,
    }
