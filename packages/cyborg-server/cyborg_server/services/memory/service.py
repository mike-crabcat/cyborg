"""MemoryService v7 — claim-centric memory system.

Claims are the source of truth. Entity records are identity-only.
Rendered views are generated from claims via templates (no LLM).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.memory.claim_types import (
    ENTITY_REF_CLAIM_KEYS,
    ENTITY_TYPE_REGISTRY,
    FOLLOW_FOR_BULLETINS_PREFIXES,
    SKIP_NEW_PATTERNS,
    detect_entity_type,
    detect_entity_types_in_text,
)
from cyborg_server.services.memory.models import (
    Bulletin,
    Claim,
    EntityDocument,
)
from cyborg_server.services.memory.claim_types import (
    render_entity,
    ENTITY_TYPES,
)
from cyborg_server.services.memory.claim_service import (
    extract_claims_from_bulletin,
    write_claim,
    get_active_claims,
    _is_valid_file_path,
)
from cyborg_server.services.memory.entity_resolver import (
    canonical_contact_id,
    normalize_entity_id,
)

logger = logging.getLogger(__name__)


class MemoryService(BaseService):
    """Reads and writes v7 memory via SQLite: bulletins, claims, entities."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._dream_task: asyncio.Task | None = None
        self._recon_task: asyncio.Task | None = None

    # ── Bulletins ─────────────────────────────────────────────────

    async def write_bulletin(
        self,
        workspace_dir: Path,
        *,
        channel_id: str,
        source_type: str,
        source_id: str = "",
        content: str,
        visibility: str = "private",
        occurred_at: str | None = None,
        session_range_start: str = "",
        session_range_end: str = "",
    ) -> str:
        """Write an immutable plain-text bulletin to the database."""
        now = utcnow()
        date_str = now.strftime("%Y-%m-%d")
        bulletin_id = f"bulletin-{date_str}-{uuid.uuid4().hex[:6]}"
        ts = occurred_at or now.isoformat()

        await self.db.execute(
            "INSERT INTO memory_bulletins "
            "(id, created_at, channel_id, source_type, source_id, visibility, content, "
            " session_range_start, session_range_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bulletin_id,
                ts,
                channel_id,
                source_type,
                source_id,
                visibility,
                content,
                session_range_start,
                session_range_end,
            ),
        )

        logger.info("Bulletin written: %s", bulletin_id)
        self._schedule_dream(workspace_dir)
        return bulletin_id

    def _schedule_dream(self, workspace_dir: Path) -> None:
        """Debounced dream trigger — runs dream 2s after last bulletin write."""
        if self._dream_task and not self._dream_task.done():
            self._dream_task.cancel()

        async def _run() -> None:
            await asyncio.sleep(2)
            try:
                result = await self.run_dream(workspace_dir)
                if result["status"] != "empty":
                    logger.info(
                        "Immediate dream processed %d bulletin(s)",
                        result["bulletins_processed"],
                    )
                    import json as _json
                    await self.db.execute(
                        "INSERT INTO memory_dream_log "
                        "(id, bulletins_processed, entries_created, bulletin_slugs, "
                        "operations_json, raw_response, duration_seconds, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), result["bulletins_processed"],
                         result.get("entity_ops", 0),
                         _json.dumps(result.get("bulletin_slugs", [])),
                         _json.dumps(result.get("operations", [])),
                         None, result.get("duration_seconds"), result["status"]),
                    )
            except Exception:
                logger.exception("Immediate dream failed")

        self._dream_task = asyncio.create_task(_run())

    def _schedule_reconciliation(self, entity_ids: list[str]) -> None:
        """Debounced supplement + reconciliation — runs 2s after last dream completes."""
        if self._recon_task and not self._recon_task.done():
            self._recon_task.cancel()

        async def _run() -> None:
            await asyncio.sleep(2)
            if not entity_ids:
                return
            try:
                from cyborg_server.services.memory.reconciliation import reconcile_entity, deprecate_file_entities_without_path
                from cyborg_server.services.llm_dispatch import LLMDispatchService
                llm = LLMDispatchService(self.ctx)

                # Phase 0: deprecate file entities with no valid file_path
                deprecated = await deprecate_file_entities_without_path(self.db)
                if deprecated:
                    logger.info("Deprecated %d file entities with no valid file_path", len(deprecated))

                # Phase 1: supplement — gap-fill from related bulletins
                workspace = self.ctx.settings.harness.workspace_dir
                for eid in entity_ids:
                    result = await self.supplement_entity(workspace, entity_id=eid)
                    if result.get("claims_added"):
                        logger.info(
                            "Supplemented %s: %d claims added from %d bulletins",
                            eid, result["claims_added"], result["bulletins_scanned"],
                        )

                # Phase 2: reconcile — consistency check on now-complete data
                for eid in entity_ids:
                    result = await reconcile_entity(
                        self.db, llm, eid,
                        update_fts_fn=self._update_entity_fts,
                        schedule_reconciliation_fn=self._schedule_reconciliation,
                    )
                    if result.get("operations_applied") or result.get("questions_raised"):
                        logger.info(
                            "Reconciled %s: %d ops, %d questions",
                            eid,
                            len(result.get("operations_applied", [])),
                            len(result.get("questions_raised", [])),
                        )
            except Exception:
                logger.exception("Supplement/reconciliation failed")

        self._recon_task = asyncio.create_task(_run())

    async def reconcile_entities(
        self, workspace_dir: Path, *, entity_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Manually trigger reconciliation for specific or all active entities."""
        from cyborg_server.services.memory.reconciliation import reconcile_entity, deprecate_file_entities_without_path
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)

        # Deprecate file entities with no valid file_path
        deprecated = await deprecate_file_entities_without_path(self.db)

        if entity_ids is None:
            rows = await self.db.fetch_all(
                "SELECT entity_id FROM memory_entities WHERE status = 'active'"
            )
            entity_ids = [r["entity_id"] for r in rows]

        results = []
        for eid in entity_ids:
            result = await reconcile_entity(
                self.db, llm, eid,
                update_fts_fn=self._update_entity_fts,
                schedule_reconciliation_fn=self._schedule_reconciliation,
            )
            results.append(result)

        return {
            "entities_checked": len(results),
            "total_issues": sum(len(r.get("issues", [])) for r in results),
            "total_ops": sum(len(r.get("operations_applied", [])) for r in results),
            "total_questions": sum(len(r.get("questions_raised", [])) for r in results),
            "details": results,
        }

    async def answer_question(
        self, workspace_dir: Path, question_id: str, answer: str,
    ) -> dict[str, Any]:
        """Answer a reconciliation question and queue the entity for re-reconciliation."""
        row = await self.db.fetch_one(
            "SELECT id, entity_id, question FROM memory_questions WHERE id = ? AND status = 'open'",
            (question_id,),
        )
        if not row:
            return {"status": "not_found"}

        entity_id = row["entity_id"]
        now = datetime.now().isoformat()

        await self.db.execute(
            "UPDATE memory_questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
            (answer, now, question_id),
        )

        # Write answer as a truth claim on the entity so reconciliation can use it
        claim = Claim(
            id=f"claim-answer-{uuid.uuid4().hex[:8]}",
            claim_type_key="truth",
            subject_id=entity_id,
            value=f"[Q: {row['question']}] {answer}",
            status="active",
            source_bulletins=[],
            created_at=datetime.now(),
        )
        await write_claim(self.db, claim)

        await self.db.execute(
            "UPDATE memory_questions SET answer_claim_id = ? WHERE id = ?",
            (claim.id, question_id),
        )

        # Queue entity for re-reconciliation
        self._schedule_reconciliation([entity_id])

        return {"status": "answered", "question_id": question_id, "claim_id": claim.id}

    async def dismiss_question(self, question_id: str) -> dict[str, Any]:
        """Dismiss a question without answering it."""
        row = await self.db.fetch_one(
            "SELECT id FROM memory_questions WHERE id = ? AND status = 'open'",
            (question_id,),
        )
        if not row:
            return {"status": "not_found"}

        await self.db.execute(
            "UPDATE memory_questions SET status = 'dismissed', answered_at = ? WHERE id = ?",
            (datetime.now().isoformat(), question_id),
        )
        return {"status": "dismissed", "question_id": question_id}

    async def read_bulletins(
        self,
        workspace_dir: Path,
        *,
        limit: int = 50,
        from_date: str | None = None,
        skip_digested: bool = False,
        oldest_first: bool = False,
    ) -> list[Bulletin]:
        """Read bulletins from the database."""
        query = "SELECT * FROM memory_bulletins"
        conditions: list[str] = []
        params: list[Any] = []

        if skip_digested:
            conditions.append("digested = 0")
        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += f" ORDER BY created_at {'ASC' if oldest_first else 'DESC'} LIMIT ?"
        params.append(limit)

        rows = await self.db.fetch_all(query, tuple(params))
        if not rows:
            return []

        results: list[Bulletin] = []
        for row in rows:
            results.append(Bulletin(
                id=row["id"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
                channel_id=row["channel_id"] or "",
                source_type=row["source_type"] or "",
                source_id=row["source_id"] or "",
                visibility=row["visibility"] or "channel",
                content=row["content"] or "",
            ))
        return results

    async def read_bulletin(self, workspace_dir: Path, bulletin_id: str) -> Bulletin | None:
        """Read a specific bulletin by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM memory_bulletins WHERE id = ?",
            (bulletin_id,),
        )
        if not row:
            return None
        return Bulletin(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            channel_id=row["channel_id"] or "",
            source_type=row["source_type"] or "",
            source_id=row["source_id"] or "",
            visibility=row["visibility"] or "channel",
            content=row["content"] or "",
        )

    # ── Session bulletin generation ───────────────────────────────

    async def generate_session_bulletins(
        self,
        workspace_dir: Path,
        session_key: str,
        *,
        active_from: str = "1970-01-01",
        limit: int = 100,
        run_dream: bool = True,
    ) -> dict[str, Any]:
        """Generate bulletins for a session from recent messages."""
        from cyborg_server.services.memory.bulletin_generator import (
            build_generator_input,
            generate_bulletins,
        )
        from cyborg_server.services.memory.channels import (
            derive_visibility,
            resolve_channel_id,
        )
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        if active_from == "1970-01-01":
            row = await self.db.fetch_one(
                "SELECT MAX(session_range_end) AS active_from FROM memory_bulletins "
                "WHERE source_id = ? AND session_range_end != ''",
                (session_key,),
            )
            active_from = row["active_from"] if row and row["active_from"] else "1970-01-01"

        last_msg = await self.db.fetch_one(
            "SELECT MAX(created_at) AS last_at FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant')",
            (session_key,),
        )
        active_to = last_msg["last_at"] if last_msg and last_msg["last_at"] else None
        if not active_to:
            return {"status": "empty", "bulletins_generated": 0, "reason": "no messages"}

        rows = await self.db.fetch_all(
            "SELECT role, content, sender_id, created_at FROM session_messages "
            "WHERE session_key = ? AND created_at > ? AND created_at <= ? "
            "AND role IN ('user', 'assistant') ORDER BY created_at ASC",
            (session_key, active_from, active_to),
        )
        if not rows:
            return {"status": "empty", "bulletins_generated": 0, "reason": "no new messages"}

        messages = [dict(r) for r in rows]
        if sum(len(m.get("content", "") or "") for m in messages) < 50:
            return {"status": "empty", "bulletins_generated": 0, "reason": "content too short"}

        participant_rows = await self.db.fetch_all(
            "SELECT contact_id, identifier, display_name FROM session_participants "
            "WHERE session_key = ?",
            (session_key,),
        )
        contact_to_name: dict[str, str] = {}
        for r in participant_rows:
            name = r["display_name"]
            if name and r["contact_id"]:
                contact_to_name[r["contact_id"]] = name

        participants = [
            {"id": canonical_contact_id(cid), "name": name}
            for cid, name in contact_to_name.items()
        ]

        gen_input = build_generator_input(
            session_key=session_key,
            messages=[
                {
                    "sender_contact_id": m.get("sender_id", "assistant"),
                    "timestamp": m.get("created_at", ""),
                    "content": (m.get("content") or "")[:500],
                }
                for m in messages[-limit:]
            ],
            participants=participants,
        )

        llm = LLMDispatchService(self.ctx)
        bulletin_texts = await generate_bulletins(llm, gen_input)

        channel_id = resolve_channel_id(session_key)
        visibility = derive_visibility(session_key)

        if not bulletin_texts:
            bulletin_id = await self.write_bulletin(
                workspace_dir,
                channel_id=channel_id,
                source_type="session",
                source_id=session_key,
                content="",
                visibility=visibility,
                occurred_at=active_to,
                session_range_start=active_from,
                session_range_end=active_to,
            )
            await self.db.execute(
                "UPDATE memory_bulletins SET digested = 1 WHERE id = ?",
                (bulletin_id,),
            )
            await self.ensure_group_entity(
                workspace_dir, session_key=session_key, bulletin_id=bulletin_id,
            )
            return {"status": "empty", "bulletins_generated": 0, "reason": "nothing memory-worthy"}

        bulletin_ids = []
        for text in bulletin_texts:
            bid = await self.write_bulletin(
                workspace_dir,
                channel_id=channel_id,
                source_type="session",
                source_id=session_key,
                content=text,
                visibility=visibility,
                occurred_at=active_to,
                session_range_start=active_from,
                session_range_end=active_to,
            )
            bulletin_ids.append(bid)
            await self.ensure_group_entity(
                workspace_dir, session_key=session_key, bulletin_id=bid,
            )

        result: dict[str, Any] = {
            "status": "ok",
            "bulletins_generated": len(bulletin_texts),
            "bulletin_ids": bulletin_ids,
            "messages_processed": len(messages),
            "active_from": active_from,
            "active_to": active_to,
        }

        if run_dream:
            dream_result = await self.run_dream(workspace_dir)
            result["dream"] = dream_result

        return result

    # ── Entities ──────────────────────────────────────────────────

    async def write_entity(self, workspace_dir: Path, entity: EntityDocument) -> str:
        """Write an entity record (identity only) to the database."""
        now = utcnow()
        status = entity.status if entity.status in ("active", "archived") else "active"

        await self.db.execute(
            "INSERT OR REPLACE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entity.entity_id,
                entity.entity_type,
                entity.display_name,
                status,
                now.isoformat(),
            ),
        )

        # Update aliases
        await self.db.execute(
            "DELETE FROM memory_aliases WHERE entity_id = ?",
            (entity.entity_id,),
        )
        alias_params = []
        if entity.display_name:
            alias_params.append((entity.display_name, entity.entity_id))
            alias_params.append((entity.display_name.lower(), entity.entity_id))
        if alias_params:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO memory_aliases (alias, entity_id) VALUES (?, ?)",
                alias_params,
            )

        # Update entity↔bulletin join rows
        if entity.source_bulletins:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO memory_entity_bulletins (entity_id, bulletin_id) VALUES (?, ?)",
                [(entity.entity_id, bid) for bid in entity.source_bulletins],
            )

        # Render and update FTS
        await self._update_entity_fts(entity.entity_id)

        logger.info("Entity written: %s/%s", entity.entity_type, entity.entity_id)
        return entity.entity_id

    async def read_entity(self, workspace_dir: Path, entity_id: str) -> EntityDocument | None:
        """Read an entity record by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        if not row:
            return None

        return EntityDocument(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            display_name=row["display_name"] or "",
            status=row["status"] or "active",
            source_bulletins=[],  # Not stored on entity in v7
        )

    async def list_entities(self, workspace_dir: Path, entity_type: str) -> list[EntityDocument]:
        """List all entities of a given type."""
        rows = await self.db.fetch_all(
            "SELECT * FROM memory_entities WHERE entity_type = ? ORDER BY entity_id",
            (entity_type,),
        )
        return [
            EntityDocument(
                entity_id=row["entity_id"],
                entity_type=entity_type,
                display_name=row["display_name"] or "",
                status=row["status"] or "active",
            )
            for row in rows
        ]

    async def _update_entity_fts(self, entity_id: str) -> None:
        """Render entity claims via template and update the FTS index."""
        entity_row = await self.db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        if not entity_row:
            return

        claims = await self.db.fetch_all(
            "SELECT claim_type_key, object_id, value FROM memory_claims "
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
            db=self.db,
        )
        await self.db.execute(
            "DELETE FROM memory_entities_fts WHERE entity_id = ?",
            (entity_id,),
        )
        await self.db.execute(
            "INSERT INTO memory_entities_fts(entity_id, display_name, rendered_body) "
            "VALUES (?, ?, ?)",
            (entity_id, entity_row["display_name"], rendered),
        )

        # Upsert embedding for semantic search
        try:
            from cyborg_server.services.memory.embedding import embed_text, upsert_embedding
            embedding = await embed_text(rendered)
            if embedding:
                await upsert_embedding(self.db, entity_id, embedding)
        except Exception:
            pass  # Non-critical — embedding failures shouldn't block FTS updates

    # ── Dream Process ─────────────────────────────────────────────

    async def process_bulletin(self, workspace_dir: Path, bulletin: Bulletin) -> dict[str, Any]:
        """Process a single bulletin: extract claims, ensure entities exist, update FTS."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)

        group_entity_id = await self._resolve_group_entity_id(bulletin.source_id)

        directory = await self._get_contact_directory()
        contact_roster = self._format_contact_roster(directory)
        group_members = await self._load_group_members(bulletin.source_id) if group_entity_id else ""
        group_members_str = self._format_group_members(directory, group_members) if group_members else ""

        # Pre-map {{contact:HEX8|Name}} tags to {{person-slug|Name}}
        contact_map = self._build_contact_to_person_map(contact_roster) if contact_roster else {}
        premapped_content = self._premap_contact_tags(bulletin.content, contact_map) if contact_map else bulletin.content

        # Detect entity types present in bulletin for claim type injection
        entity_types_hint = self._detect_entity_types_in_text(premapped_content)

        claims = await extract_claims_from_bulletin(
            llm, bulletin,
            entity_types_in_bulletin=entity_types_hint,
            known_group_entity_id=group_entity_id,
            contact_roster=contact_roster,
            group_members=group_members_str,
            db=self.db,
            premapped_content=premapped_content,
        )

        wrote_claims = 0
        for claim in claims:
            await write_claim(self.db, claim)
            wrote_claims += 1

        # Ensure entity records exist for all claim subjects
        entity_ids = await self._ensure_entities_for_claims(claims, bulletin)

        # Update FTS for all touched entities
        for eid in entity_ids:
            await self._update_entity_fts(eid)

        return {
            "bulletin_id": bulletin.id,
            "claims_extracted": wrote_claims,
            "claims": [
                {
                    "id": c.id,
                    "claim_type_key": c.claim_type_key,
                    "subject_id": c.subject_id,
                    "object_id": c.object_id,
                    "value": c.value,
                }
                for c in claims
            ],
            "entities_updated": entity_ids,
        }

    async def run_dream(self, workspace_dir: Path) -> dict[str, Any]:
        """Process all pending (undigested) bulletins through the dream pipeline."""
        bulletins = await self.read_bulletins(workspace_dir, skip_digested=True, oldest_first=True, limit=10000)
        if not bulletins:
            return {"status": "empty", "bulletins_processed": 0}

        logger.info("Memory dream: processing %d bulletins (oldest first)", len(bulletins))
        start = datetime.now().timestamp()

        total_claims = 0
        ops_detail: list[dict[str, Any]] = []

        for bulletin in bulletins:
            result = await self.process_bulletin(workspace_dir, bulletin)
            total_claims += result["claims_extracted"]
            ops_detail.append({
                "bulletin": bulletin.id,
                "source": bulletin.source_id or "",
                "claims": result["claims_extracted"],
                "entities_updated": result.get("entities_updated", []),
                "content_preview": (bulletin.content or "")[:120],
            })
            await self._mark_digested(bulletin)

        elapsed = datetime.now().timestamp() - start

        # Schedule reconciliation for all touched entities
        all_entity_ids: set[str] = set()
        for op in ops_detail:
            for eid in op.get("entities_updated", []):
                all_entity_ids.add(eid)
        if all_entity_ids:
            self._schedule_reconciliation(list(all_entity_ids))

        return {
            "status": "completed",
            "bulletins_processed": len(bulletins),
            "bulletin_slugs": [b.id for b in bulletins],
            "claims_extracted": total_claims,
            "operations": ops_detail,
            "duration_seconds": round(elapsed, 1),
        }

    @staticmethod
    def _detect_entity_types_in_text(text: str) -> list[str]:
        """Detect likely entity types mentioned in text for claim type injection."""
        return detect_entity_types_in_text(text)

    async def _ensure_entities_for_claims(
        self, claims: list[Claim], bulletin: Bulletin
    ) -> list[str]:
        """Ensure entity records exist for all claim subject/object IDs."""
        _skip_patterns = SKIP_NEW_PATTERNS + ("transport-",)
        entity_ids: set[str] = set()
        for c in claims:
            for attr in ("subject_id", "object_id"):
                val = getattr(c, attr)
                if not val or any(val.startswith(p) for p in _skip_patterns):
                    continue
                entity_ids.add(val)

        # File entities require a valid file_path claim — skip otherwise.
        file_ids_with_path: set[str] = set()
        for c in claims:
            if c.claim_type_key == "file_path" and c.value and _is_valid_file_path(c.value):
                file_ids_with_path.add(c.subject_id)
        entity_ids = {
            eid for eid in entity_ids
            if not eid.startswith("file-") or eid in file_ids_with_path
        }

        existing = await self._list_all_entity_ids()
        created: list[str] = []

        for eid in entity_ids:
            if eid not in existing:
                etype = detect_entity_type(eid)
                display_name = await self._resolve_display_name(eid)
                entity = EntityDocument(
                    entity_id=eid,
                    entity_type=etype,
                    display_name=display_name,
                    status="active",
                    source_bulletins=[bulletin.id],
                )
                await self.write_entity(Path("."), entity)
                created.append(eid)

        return list(entity_ids)

    async def _resolve_display_name(self, entity_id: str) -> str:
        """Try to resolve a display name for an entity ID."""
        etype = detect_entity_type(entity_id)
        et_def = ENTITY_TYPE_REGISTRY.get(etype)

        if et_def and et_def.display_name_claim:
            rows = await self.db.fetch_all(
                "SELECT value FROM memory_claims WHERE subject_id = ? "
                "AND claim_type_key = ? AND status = 'active' LIMIT 1",
                (entity_id, et_def.display_name_claim),
            )
            if rows and rows[0]["value"]:
                hex8 = rows[0]["value"][:8]
                row = await self.db.fetch_one(
                    "SELECT name FROM contacts WHERE id LIKE ? LIMIT 1",
                    (f"{hex8}%",),
                )
                if row and row["name"]:
                    return row["name"]
            slug = entity_id.removeprefix(et_def.prefix)
            return " ".join(part.capitalize() for part in slug.split("-"))

        return entity_id

    async def _list_all_entity_ids(self) -> set[str]:
        """List all entity IDs."""
        rows = await self.db.fetch_all("SELECT entity_id FROM memory_entities")
        return {r["entity_id"] for r in rows}

    # ── Retrieval ─────────────────────────────────────────────────

    async def search_entries(
        self, workspace_dir: Path, query: str, entity_type: str = ""
    ) -> dict[str, Any]:
        """Search memory using FTS5 across rendered entity bodies."""
        from cyborg_server.services.memory.tools import find

        if entity_type:
            results = await find(self.db, entity_type)
            return {"abstract": results, "results": []}

        # Build FTS query: AND individual tokens for broad matching
        tokens = query.strip().split()
        if not tokens:
            return {"abstract": "", "results": []}
        fts_parts = []
        for t in tokens:
            escaped = t.replace('"', '""')
            fts_parts.append(f'"{escaped}"')
        fts_query = " AND ".join(fts_parts)

        fts_rows = await self.db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities_fts "
            "WHERE memory_entities_fts MATCH ? LIMIT 20",
            (fts_query,),
        )

        # If FTS found nothing, try embedding search
        if not fts_rows:
            try:
                from cyborg_server.services.memory.embedding import search_similar
                emb_results = await search_similar(self.db, query, limit=10, threshold=1.2)
                if emb_results:
                    entity_ids = [r["entity_id"] for r in emb_results]
                    placeholders = ",".join("?" for _ in entity_ids)
                    emb_rows = await self.db.fetch_all(
                        f"SELECT e.entity_id, e.display_name, e.entity_type "
                        f"FROM memory_entities e WHERE e.entity_id IN ({placeholders}) AND e.status = 'active'",
                        tuple(entity_ids),
                    )
                    # Preserve distance ordering
                    row_map = {r["entity_id"]: r for r in emb_rows}
                    fts_rows = [row_map[eid] for eid in entity_ids if eid in row_map]
            except Exception:
                pass

        if not fts_rows:
            return {"abstract": f"No entities found matching: {query}", "results": []}

        results = [
            {
                "path": f"memory/{r['entity_id']}.md",
                "title": r["display_name"] or r["entity_id"],
                "relevance": "",
            }
            for r in fts_rows
        ]
        abstract = f"Found {len(results)} entities matching '{query}'"
        return {"abstract": abstract, "results": results}

    async def build_memory_index(self, workspace_dir: Path) -> str:
        """Build compact memory index for system prompt injection."""
        return await build_memory_index_text_db(self.db)

    async def merge_entities(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Detect and merge duplicate entities using embeddings + LLM."""
        from cyborg_server.services.memory.merge import run_merge
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        return await run_merge(self.db, llm, dry_run=dry_run)

    async def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from scratch. Returns row count."""
        rows = await self.db.fetch_all("SELECT entity_id FROM memory_entities")
        for r in rows:
            await self._update_entity_fts(r["entity_id"])
        row = await self.db.fetch_one("SELECT count(*) AS c FROM memory_entities_fts")
        return row["c"] if row else 0

    async def rebuild_embeddings(self) -> int:
        """Rebuild embedding vectors for all entities. Returns count."""
        from cyborg_server.services.memory.embedding import embed_batch, upsert_embedding

        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE status = 'active'"
        )
        if not rows:
            return 0

        # Render all entities first
        rendered_map: dict[str, str] = {}
        for r in rows:
            claims = await self.db.fetch_all(
                "SELECT claim_type_key, object_id, value FROM memory_claims "
                "WHERE status = 'active' AND subject_id = ?",
                (r["entity_id"],),
            )
            claim_dicts = [
                {"claim_type_key": c["claim_type_key"], "object_id": c["object_id"], "value": c["value"]}
                for c in claims
            ]
            rendered_map[r["entity_id"]] = await render_entity(r["entity_type"], r["display_name"], claim_dicts, entity_id=r["entity_id"], db=self.db)

        # Batch embed (up to 100 at a time)
        entity_ids = list(rendered_map.keys())
        count = 0
        batch_size = 100
        for i in range(0, len(entity_ids), batch_size):
            batch_ids = entity_ids[i:i + batch_size]
            batch_texts = [rendered_map[eid] for eid in batch_ids]
            embeddings = await embed_batch(batch_texts)
            for eid, emb in zip(batch_ids, embeddings):
                if emb:
                    await upsert_embedding(self.db, eid, emb)
                    count += 1

        logger.info("Embedded %d entities", count)
        return count

    # ── Person/Contact helpers ────────────────────────────────────

    async def _get_contact_directory(self):
        """Load and cache ContactDirectory."""
        from cyborg_server.services.memory.contact_directory import ContactDirectory
        cache = getattr(self, "_contact_dir_cache", None)
        if cache is None and self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
            cache = await ContactDirectory.load(self.ctx.db)
            self._contact_dir_cache = cache
        return cache

    @staticmethod
    def _format_contact_roster(directory: Any) -> str:
        """Format ContactDirectory as a person roster for the LLM prompt.

        Maps contact-{hex8} IDs to person-{slug} IDs so the LLM knows
        which person entity to use for each known contact.
        """
        if directory is None:
            return ""
        import re
        lines = []
        for record in directory._by_canonical.values():
            name = record.name
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            lines.append(f"- {record.canonical_id} ({name}) → {person_id}")
        return "\n".join(lines)

    @staticmethod
    def _build_contact_to_person_map(roster_text: str) -> dict[str, str]:
        """Parse the roster into a contact-{hex8} → person-{slug} map."""
        mapping: dict[str, str] = {}
        for line in roster_text.split("\n"):
            # Format: "- contact-{hex8} (Name) → person-{slug}"
            m = __import__("re").match(r"^- (contact-[a-f0-9]+) \((.+?)\) → (person-[\w-]+)$", line.strip())
            if m:
                mapping[m.group(1)] = m.group(3)
        return mapping

    @staticmethod
    def _premap_contact_tags(text: str, contact_map: dict[str, str]) -> str:
        """Replace {{contact:HEX8|Name}} tags with {{person-slug|Name}} in bulletin text."""
        import re
        def _replace(m: re.Match) -> str:
            hex8 = m.group(1)[:8]
            name = m.group(2)
            contact_id = f"contact-{hex8}"
            person_id = contact_map.get(contact_id, "")
            if person_id:
                return f"{{{{{person_id}|{name}}}}}"
            # Fallback: derive slug from name
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            return f"{{{{person-{slug}|{name}}}}}"
        return re.sub(r"\{\{contact:([a-f0-9-]+)\|(.+?)\}\}", _replace, text)

    async def _load_group_members(self, source_id: str) -> list[str] | None:
        """Load group member canonical contact IDs for a session."""
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None
        rows = await self.db.fetch_all(
            "SELECT gm.contact_id FROM whatsappgroup_members gm "
            "JOIN contacts c ON c.id = gm.contact_id "
            "WHERE gm.group_id = (SELECT id FROM whatsappgroups WHERE whatsapp_jid = ?) "
            "AND gm.left_at IS NULL",
            (route["chat_id"],),
        )
        return [f"contact-{str(r['contact_id'])[:8]}" for r in rows]

    @staticmethod
    def _format_group_members(directory: Any, member_ids: list[str]) -> str:
        """Format group member list for the LLM prompt using person-{slug} IDs."""
        if not member_ids or directory is None:
            return ""
        import re
        parts = []
        for mid in member_ids:
            record = directory.get_by_canonical_id(mid)
            name = record.name if record else mid
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            parts.append(f"{person_id} ({name})")
        return ", ".join(parts)

    async def ensure_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str,
        name: str,
        phone_number: str = "",
        email: str = "",
        channel: str = "",
    ) -> str | None:
        """Create a minimal person entity record if one doesn't exist."""
        import re
        slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
        person_id = f"person-{slug}"

        existing = await self.read_entity(workspace_dir, person_id)
        if existing:
            return person_id

        entity = EntityDocument(
            entity_id=person_id,
            entity_type="person",
            display_name=name,
            status="active",
        )
        await self.write_entity(workspace_dir, entity)

        # Write a contact_id claim linking person to contacts table row
        hex8 = contact_id[:8]
        from cyborg_server.services.memory.claim_service import write_claim
        claim = Claim(
            id=f"claim-person-{person_id}-contact_id",
            claim_type_key="contact_id",
            subject_id=person_id,
            value=hex8,
            status="active",
            visibility="private",
        )
        await write_claim(self.db, claim)

        return person_id

    async def find_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str = "",
        name: str = "",
    ) -> str | None:
        """Find a person entity by name (slug) or by contact_id claim."""
        if name:
            import re
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            entity = await self.read_entity(workspace_dir, person_id)
            if entity:
                return entity.entity_id
        if contact_id:
            hex8 = contact_id[:8]
            rows = await self.db.fetch_all(
                "SELECT subject_id FROM memory_claims "
                "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
                (hex8,),
            )
            if rows:
                return rows[0]["subject_id"]
        return None

    # ── Group helpers ─────────────────────────────────────────────

    async def _resolve_group_entity_id(self, source_id: str) -> str | None:
        """Look up the group entity ID for a bulletin's source session."""
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None
        row = await self.db.fetch_one(
            "SELECT memory_entity_id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (route["chat_id"],),
        )
        return row["memory_entity_id"] if row and row["memory_entity_id"] else None

    async def ensure_group_entity(
        self,
        workspace_dir: Path,
        session_key: str,
        bulletin_id: str,
    ) -> str | None:
        """Ensure a group entity exists for a group session and link the bulletin."""
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None

        chat_id = route["chat_id"]

        group_row = await self.db.fetch_one(
            "SELECT id, name, description, memory_entity_id, member_count "
            "FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (chat_id,),
        )
        if not group_row:
            return None

        group_name = group_row["name"] or chat_id
        existing_entity_id = group_row["memory_entity_id"]

        if existing_entity_id:
            entity = await self.read_entity(workspace_dir, existing_entity_id)
            if entity and entity.display_name != group_name:
                entity.display_name = group_name
                await self.write_entity(workspace_dir, entity)
        else:
            entity_id = f"group-{uuid.uuid4().hex[:8]}"
            entity = EntityDocument(
                entity_id=entity_id,
                entity_type="group",
                display_name=group_name,
                status="active",
            )
            await self.write_entity(workspace_dir, entity)
            existing_entity_id = entity_id

            await self.db.execute(
                "UPDATE whatsappgroups SET memory_entity_id = ? WHERE id = ?",
                (entity_id, group_row["id"]),
            )

        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entity_bulletins (entity_id, bulletin_id) VALUES (?, ?)",
            (existing_entity_id, bulletin_id),
        )

        return existing_entity_id

    # ── Rebuild ───────────────────────────────────────────────────

    async def rebuild(self, workspace_dir: Path, *, entity_id: str | None = None, all: bool = False) -> dict[str, Any]:
        """Rebuild derived data from bulletins."""
        if entity_id:
            return await self._rebuild_entity(workspace_dir, entity_id)

        if all:
            await self.db.execute("DELETE FROM memory_claims")
            await self.db.execute("DELETE FROM memory_claim_bulletins")
            await self.db.execute("DELETE FROM memory_entity_relations")
            await self.db.execute("DELETE FROM memory_entity_bulletins")
            await self.db.execute("DELETE FROM memory_aliases")
            await self.db.execute("DELETE FROM memory_entities_fts")
            await self.db.execute("DELETE FROM memory_entity_embeddings")
            await self.db.execute("DELETE FROM memory_entities")
            await self.db.execute("DELETE FROM memory_questions")
            await self.db.execute("UPDATE memory_bulletins SET digested = 0")

            bulletins = await self.read_bulletins(workspace_dir, limit=10000, oldest_first=True)
            total_claims = 0
            for bulletin in bulletins:
                result = await self.process_bulletin(workspace_dir, bulletin)
                total_claims += result["claims_extracted"]

            # Rebuild embeddings
            embed_count = await self.rebuild_embeddings()

            # Reconcile trips — catches merged stays, overlapping dates, etc.
            trip_rows = await self.db.fetch_all(
                "SELECT entity_id FROM memory_entities WHERE entity_type = 'trip' AND status = 'active'"
            )
            recon_result = await self.reconcile_entities(
                workspace_dir, entity_ids=[r["entity_id"] for r in trip_rows],
            )

            return {
                "status": "completed",
                "bulletins_processed": len(bulletins),
                "claims": total_claims,
                "embeddings_rebuilt": embed_count,
                "reconciliation": {
                    "issues": recon_result["total_issues"],
                    "operations": recon_result["total_ops"],
                    "questions": recon_result["total_questions"],
                },
            }

        return {"status": "no_op"}

    async def _rebuild_entity(self, workspace_dir: Path, entity_id: str) -> dict[str, Any]:
        """Rebuild a single entity by reprocessing its linked bulletins."""
        rows = await self.db.fetch_all(
            "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
            (entity_id,),
        )
        bulletin_ids = [r["bulletin_id"] for r in rows]
        if not bulletin_ids:
            return {"status": "no_op", "reason": "no linked bulletins"}

        for bid in bulletin_ids:
            await self.db.execute(
                "DELETE FROM memory_claims WHERE source_bulletins LIKE ?",
                (f'%{bid}%',),
            )

        await self.db.execute("DELETE FROM memory_entity_relations WHERE source_entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_entity_bulletins WHERE entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_aliases WHERE entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_entities WHERE entity_id = ?", (entity_id,))

        await self.db.execute(
            "UPDATE memory_bulletins SET digested = 0 WHERE id IN ({})".format(
                ",".join("?" for _ in bulletin_ids)
            ),
            tuple(bulletin_ids),
        )

        bulletins = await self.read_bulletins(workspace_dir, limit=10000, skip_digested=True, oldest_first=True)
        bulletin_set = set(bulletin_ids)
        relevant = [b for b in bulletins if b.id in bulletin_set]
        if not relevant:
            return {"status": "no_op", "reason": "no undigested bulletins found"}

        total_claims = 0
        for bulletin in relevant:
            result = await self.process_bulletin(workspace_dir, bulletin)
            total_claims += result["claims_extracted"]

        return {"status": "completed", "entity_id": entity_id, "bulletins_processed": len(relevant), "claims": total_claims}

    # ── Supplement (gap-fill) ────────────────────────────────────────

    async def _collect_related_bulletins(self, entity_id: str) -> list[str]:
        """Collect bulletin IDs from an entity and its related entities.

        Walks ENTITY_REF_CLAIM_KEYS to find parent trip, sibling stays,
        linked transports, locations etc. Also follows the chain two hops
        (e.g. stay → trip → sibling stay bulletins).
        Includes bulletins from the same source threads as any related bulletin.
        """
        # Directly-linked bulletins
        rows = await self.db.fetch_all(
            "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
            (entity_id,),
        )
        bulletin_ids: set[str] = {r["bulletin_id"] for r in rows}

        # Also pull bulletin IDs from claim source_bulletins JSON arrays
        claim_src_rows = await self.db.fetch_all(
            "SELECT source_bulletins FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ? AND source_bulletins IS NOT NULL",
            (entity_id,),
        )
        for r in claim_src_rows:
            try:
                bids = json.loads(r["source_bulletins"]) if r["source_bulletins"] else []
                bulletin_ids.update(bids)
            except (json.JSONDecodeError, TypeError):
                pass

        # Walk entity-ref claims to find related entities
        claims = await self.db.fetch_all(
            "SELECT claim_type_key, value, object_id FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ? AND claim_type_key IN ({})".format(
                ",".join("?" for _ in ENTITY_REF_CLAIM_KEYS)
            ),
            (entity_id,) + tuple(ENTITY_REF_CLAIM_KEYS),
        )
        related_ids: set[str] = set()
        for c in claims:
            ref = c["object_id"] or c["value"] or ""
            if ref and ref.startswith(FOLLOW_FOR_BULLETINS_PREFIXES):
                related_ids.add(ref)

        # Also find entities that reference this entity (reverse direction)
        reverse_claims = await self.db.fetch_all(
            "SELECT subject_id FROM memory_claims "
            "WHERE status = 'active' AND (object_id = ? OR value = ?) AND claim_type_key IN ({})".format(
                ",".join("?" for _ in ENTITY_REF_CLAIM_KEYS)
            ),
            (entity_id, entity_id) + tuple(ENTITY_REF_CLAIM_KEYS),
        )
        for c in reverse_claims:
            related_ids.add(c["subject_id"])

        # Second hop: from related entities, find their related entities too
        if related_ids:
            hop2_placeholders = ",".join("?" for _ in related_ids)
            hop2_claims = await self.db.fetch_all(
                "SELECT claim_type_key, value, object_id FROM memory_claims "
                "WHERE status = 'active' AND subject_id IN ({}) AND claim_type_key IN ({})".format(
                    hop2_placeholders,
                    ",".join("?" for _ in ENTITY_REF_CLAIM_KEYS),
                ),
                tuple(related_ids) + tuple(ENTITY_REF_CLAIM_KEYS),
            )
            for c in hop2_claims:
                ref = c["object_id"] or c["value"] or ""
                if ref and ref.startswith(FOLLOW_FOR_BULLETINS_PREFIXES):
                    related_ids.add(ref)

        # Collect bulletins from all related entities
        if related_ids:
            placeholders = ",".join("?" for _ in related_ids)
            rows2 = await self.db.fetch_all(
                f"SELECT DISTINCT bulletin_id FROM memory_entity_bulletins WHERE entity_id IN ({placeholders})",
                tuple(related_ids),
            )
            bulletin_ids.update(r["bulletin_id"] for r in rows2)

        return sorted(bulletin_ids)

    async def supplement_entity(self, workspace_dir: Path, entity_id: str) -> dict[str, Any]:
        """Re-extract from source bulletins and only write missing claims.

        Scans bulletins from the entity itself plus related entities
        (parent trip, sibling stays, linked transports/locations).
        Uses a dedicated supplement prompt that allows inference from
        related data (unlike the strict extraction prompt).
        Non-destructive: existing claims are never modified or removed.
        """
        bulletin_ids = await self._collect_related_bulletins(entity_id)
        if not bulletin_ids:
            return {"status": "no_op", "reason": "no linked bulletins"}

        # Load current active claims for dedup
        current_rows = await self.db.fetch_all(
            "SELECT subject_id, claim_type_key, value, object_id FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ?",
            (entity_id,),
        )
        existing: set[tuple[str, str, str]] = set()
        current_lines: list[str] = []
        for r in current_rows:
            val = r["value"] or r["object_id"] or ""
            existing.add((r["subject_id"], r["claim_type_key"], val))
            current_lines.append(f"- {r['claim_type_key']}: {val}")

        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.memory.claim_service import write_claim
        from cyborg_server.services.memory.claim_types import get_claim_types_for_entity

        llm = LLMDispatchService(self.ctx)

        # Load entity info
        entity_row = await self.db.fetch_one(
            "SELECT entity_type, display_name FROM memory_entities WHERE entity_id = ? AND status = 'active'",
            (entity_id,),
        )
        if not entity_row:
            return {"status": "no_op", "reason": "entity not found"}

        entity_type = entity_row["entity_type"]

        # File entities: skip supplement if no valid file_path exists
        if entity_type == "file":
            has_path = any(
                r["claim_type_key"] == "file_path" and r["value"]
                and _is_valid_file_path(r["value"])
                for r in current_rows
            )
            if not has_path:
                logger.info("Supplement: skipping file entity %s — no valid file_path", entity_id)
                return {"status": "no_op", "reason": "file entity has no valid file_path"}

        claim_types = get_claim_types_for_entity(entity_type)

        bulletins = await self.read_bulletins(workspace_dir, limit=10000, oldest_first=True)
        bulletin_set = set(bulletin_ids)
        relevant = [b for b in bulletins if b.id in bulletin_set]
        if not relevant:
            return {"status": "no_op", "reason": "bulletins not found"}

        # Build bulletin text
        bulletin_texts = []
        for b in relevant:
            bulletin_texts.append(f"[{b.id}] {b.content}")
        all_bulletin_text = "\n\n".join(bulletin_texts)

        # Build claim type list for this entity
        ct_lines = [f"  - {ct.key}: {ct.description}" for ct in claim_types]

        current_claims_str = "\n".join(current_lines) if current_lines else "(none)"

        prompt = (
            f"You are a Memory Supplement Agent. You review bulletins and identify claims that are "
            f"missing for a specific entity. Unlike initial extraction, you MAY infer entity claims "
            f"from related information (e.g. a transport departure implies a stay departure date, "
            f"a hotel check-in implies a stay arrival).\n\n"
            f"## CRITICAL RULE\n\n"
            f"Every claim you produce must be a fact ABOUT the target entity "
            f"({entity_row['display_name']} / {entity_id}). "
            f"Do NOT extract claims about OTHER entities that happen to be mentioned in the bulletins. "
            f"For example, if the target is a person mentioned only as \"Sam (~8)\" in a family list, "
            f"do NOT extract the speaker's email, timezone, or language — those belong to the speaker, "
            f"not to Sam.\n\n"
            f"## Entity: {entity_id} ({entity_type})\n\n"
            f"Display name: {entity_row['display_name']}\n\n"
            f"## Current Claims\n\n{current_claims_str}\n\n"
            f"## Available Claim Types\n\n" + "\n".join(ct_lines) + "\n\n"
            f"## Source Bulletins\n\n{all_bulletin_text}\n\n"
            f"## Task\n\n"
            f"Identify claims from the bulletins that should belong to {entity_id} but are missing "
            f"from the current claims. You may infer dates and relationships from related data "
            f"(transport bookings, hotel confirmations, etc.).\n\n"
            f"IMPORTANT: Placeholder values containing '??' (e.g. '2026-06-??') are incomplete guesses. "
            f"If a bulletin provides a concrete value for a claim that currently has a placeholder, "
            f"include the corrected claim in your output — it is NOT considered already present.\n\n"
            f"Return a JSON array of missing claims:\n```json\n"
            f'[\n  {{"claim_type_key": "key", "value": "scalar", "object_id": null}},\n'
            f'  {{"claim_type_key": "key", "value": null, "object_id": "entity-ref"}}\n'
            f"]\n```\n\n"
            f"Use value for scalar data (dates, text) and object_id for entity references. "
            f"Never set both. If no claims are missing, return [].\n"
            f"Return ONLY the JSON array."
        )

        response = await llm.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"What claims are missing for {entity_id}?"},
            ],
            model=llm.memory_model,
            call_category="memory_supplement",
            temperature=0.1,
            max_tokens=1000,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            items = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Supplement: failed to parse LLM response for %s", entity_id)
            return {"status": "completed", "entity_id": entity_id, "bulletins_scanned": len(relevant), "claims_added": 0, "added": []}

        if not isinstance(items, list):
            items = []

        total_added = 0
        added_claims: list[dict] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            ctk = item.get("claim_type_key", "")
            val = item.get("value")
            obj = item.get("object_id")
            if not ctk:
                continue
            claim_val = val or obj or ""
            key = (entity_id, ctk, claim_val)
            if key in existing:
                # Allow upgrading placeholder values (containing '??')
                is_placeholder = any(
                    "??" in (r["value"] or r["object_id"] or "")
                    for r in current_rows
                    if r["claim_type_key"] == ctk
                )
                if not is_placeholder:
                    continue
            # Skip entity-ref claims — supplement should not infer relationships.
            # Entity-ref claims (parent, child, partner, etc.) must come from
            # explicit extraction, not supplement inference.
            if ctk in ENTITY_REF_CLAIM_KEYS:
                continue
            claim = Claim(
                id=f"claim-suppl-{uuid.uuid4().hex[:8]}",
                claim_type_key=ctk,
                subject_id=entity_id,
                value=val,
                object_id=obj,
                status="active",
                source_bulletins=bulletin_ids,
                created_at=datetime.now(),
            )
            await write_claim(self.db, claim)
            existing.add(key)
            total_added += 1
            added_claims.append({"claim_type_key": ctk, "value": val, "object_id": obj})

        if total_added:
            await self._update_entity_fts(entity_id)

        return {
            "status": "completed",
            "entity_id": entity_id,
            "bulletins_scanned": len(relevant),
            "claims_added": total_added,
            "added": added_claims,
        }

    # ── Validation ────────────────────────────────────────────────

    async def validate(self, workspace_dir: Path) -> dict[str, Any]:
        """Validate memory data."""
        issues: list[str] = []
        rows = await self.db.fetch_all(
            "SELECT entity_id FROM memory_entities WHERE display_name = '' OR entity_type = ''"
        )
        for r in rows:
            issues.append(f"{r['entity_id']}: missing display_name or entity_type")
        return {"valid": len(issues) == 0, "issues": issues}

    # ── Legacy compatibility ──────────────────────────────────────

    async def browse_category(self, workspace_dir: Path, wiki: str, category: str) -> list[dict[str, Any]]:
        """Legacy: browse entities by type."""
        return [
            {"slug": e.entity_id, "title": e.display_name, "modified": 0}
            for e in await self.list_entities(workspace_dir, category)
        ]

    async def read_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str) -> str | None:
        """Legacy: read an entity by slug."""
        entity = await self.read_entity(workspace_dir, slug)
        if entity:
            return entity.display_name
        return None

    async def write_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str, title: str, content: str) -> str:
        """Legacy: write an entity."""
        entity = EntityDocument(
            entity_id=slug,
            entity_type=category,
            display_name=title,
        )
        return await self.write_entity(workspace_dir, entity)

    async def list_recent_entries(self, workspace_dir: Path, wiki_names: list[str], limit: int = 50) -> dict[str, Any]:
        """Legacy: list recent entity documents."""
        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type, display_name, updated_at "
            "FROM memory_entities ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        entries = [
            {
                "path": r["entity_id"],
                "wiki": "core",
                "category": r["entity_type"],
                "slug": r["entity_id"],
                "title": r["display_name"] or "",
                "summary": "",
                "modified": r["updated_at"],
            }
            for r in rows
        ]
        return {
            "stats": {"total_entries": len(rows)},
            "recent": entries,
        }

    async def resolve_accessible_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    async def resolve_writable_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    def validate_wiki_category(self, workspace_dir: Path, wiki: str, category: str) -> bool:
        return True

    def move_to_digested(self, workspace_dir: Path, bulletin_paths: list[Path]) -> None:
        """Legacy: no-op."""

    async def _mark_digested(self, bulletin: Bulletin) -> None:
        """Mark a bulletin as digested."""
        await self.db.execute(
            "UPDATE memory_bulletins SET digested = 1 WHERE id = ?",
            (bulletin.id,),
        )


async def build_memory_index_text_db(db: Any) -> str:
    """Build a compact memory index from claims for system prompt injection."""
    rows = await db.fetch_all(
        "SELECT entity_type, entity_id, display_name "
        "FROM memory_entities ORDER BY entity_type, entity_id"
    )
    if not rows:
        return ""

    by_type: dict[str, list[str]] = {}
    for r in rows:
        entry_str = r["display_name"] or r["entity_id"]
        by_type.setdefault(r["entity_type"], []).append(entry_str)

    lines = [f"**{t}**: " + ", ".join(entries) for t, entries in sorted(by_type.items())]
    return "\n".join(lines)
