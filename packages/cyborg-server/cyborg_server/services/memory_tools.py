"""Memory tools for LLM function calling (v7 claim-centric).

Usage:
    tools.extend(make_memory_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging

from cyborg_server.context import AppContext
from cyborg_server.services.memory import MemoryService
from cyborg_server.services.memory.channels import resolve_channel_id
from cyborg_server.services.memory.claim_types import render_entity
from cyborg_server.services.memory.claim_service import get_active_claims
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_memory_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create memory recall/find/note tools bound to the given context."""

    svc = MemoryService(ctx)

    @tool
    async def recall(query: str) -> str:
        """Retrieve entity information by ID, name, or natural language query.
        Returns the entity's claims rendered as readable text."""
        from cyborg_server.services.memory.tools import recall as _recall
        return await _recall(ctx.db, query)

    @tool
    async def find(
        entity_type: str,
        claim_type_key: str = "",
        value: str = "",
    ) -> str:
        """Find entities by type with optional claim filters.
        Entity types: person, group, location, trip, stay, event, task, file, thing, decision.
        Returns matching entity IDs and display names."""
        from cyborg_server.services.memory.tools import find as _find
        return await _find(ctx.db, entity_type, claim_type_key or None, value or None)

    @tool
    async def note(
        text: str,
        context_entity_id: str = "",
    ) -> str:
        """Accept new information from conversation. Queues as a bulletin for digestion.
        Optionally link to a context entity ID (e.g. trip-bali-2026)."""
        from cyborg_server.services.memory.tools import note as _note
        channel_id = resolve_channel_id(session_key)
        return await _note(ctx.db, text, context_entity_id or None, channel_id=channel_id)

    @tool
    async def memory_write(
        content: str,
        channel_id: str = "",
        visibility: str = "private",
    ) -> str:
        """Create a memory bulletin. Content is markdown.
        Queued for digestion into claims. Use note() for simpler input."""
        workspace = ctx.settings.harness.workspace_dir

        cid = channel_id or resolve_channel_id(session_key)

        bulletin_id = await svc.write_bulletin(
            workspace,
            channel_id=cid,
            source_type="manual",
            source_id=session_key,
            content=content,
            visibility=visibility,
        )
        return json.dumps({"ok": True, "bulletin_id": bulletin_id, "queued": True})

    @tool
    async def memory_read(entity_id: str) -> str:
        """Read a specific memory entity by ID. Returns rendered claims."""
        workspace = ctx.settings.harness.workspace_dir

        entity = await svc.read_entity(workspace, entity_id)
        if entity is None:
            return json.dumps({"error": f"Entity not found: {entity_id}"})

        # Fetch and render claims
        claims = await get_active_claims(ctx.db, entity_id)
        claim_dicts = [
            {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
            for c in claims
        ]
        rendered = await render_entity(entity.entity_type, entity.display_name, claim_dicts, entity_id=entity.entity_id, db=ctx.db)
        return json.dumps({
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "display_name": entity.display_name,
            "rendered": rendered,
        })

    @tool
    async def memory_correct(
        action: str,
        entity_id: str = "",
        claim_type_key: str = "",
        value: str = "",
        reason: str = "",
    ) -> str:
        """Correct or remove wrong memory data. Actions:
        - "remove_entity": Archive an entity and supersede all its claims. Use for hallucinated/incorrect entities.
        - "remove_claim": Supersede a specific claim on an entity. Requires entity_id, claim_type_key, and value.
        - "set_truth": Write a truth claim on an entity (user-stated correction that overrides inference).
        Always provide a reason explaining why the correction is needed."""
        from cyborg_server.services.memory.claim_service import write_claim
        from cyborg_server.services.memory.models import Claim
        from datetime import datetime
        import uuid

        if not reason:
            return json.dumps({"error": "reason is required for all corrections"})

        if action == "remove_entity":
            if not entity_id:
                return json.dumps({"error": "entity_id is required for remove_entity"})
            # Check entity exists
            row = await ctx.db.fetch_one(
                "SELECT entity_id, entity_type FROM memory_entities WHERE entity_id = ? AND status = 'active'",
                (entity_id,),
            )
            if not row:
                return json.dumps({"error": f"Entity not found or already archived: {entity_id}"})

            # Archive the entity
            await ctx.db.execute(
                "UPDATE memory_entities SET status = 'archived' WHERE entity_id = ?",
                (entity_id,),
            )
            # Supersede all active claims
            claims = await ctx.db.fetch_all(
                "SELECT id FROM memory_claims WHERE subject_id = ? AND status = 'active'",
                (entity_id,),
            )
            for c in claims:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (c["id"],),
                )
            # Also remove claims referencing this entity as object_id
            ref_claims = await ctx.db.fetch_all(
                "SELECT id FROM memory_claims WHERE object_id = ? AND status = 'active'",
                (entity_id,),
            )
            for c in ref_claims:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (c["id"],),
                )
            # Write a truth claim to prevent re-creation
            truth_claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=f"[removed] {reason}",
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, truth_claim)

            logger.info("Entity removed via memory_correct: %s (%d claims, %d refs) — %s",
                       entity_id, len(claims), len(ref_claims), reason)
            return json.dumps({
                "ok": True,
                "action": "remove_entity",
                "entity_id": entity_id,
                "claims_archived": len(claims),
                "references_removed": len(ref_claims),
            })

        elif action == "remove_claim":
            if not entity_id or not claim_type_key:
                return json.dumps({"error": "entity_id and claim_type_key required for remove_claim"})
            # Find matching active claims
            params: list = [entity_id, claim_type_key]
            extra = ""
            if value:
                extra = " AND (value = ? OR object_id = ?)"
                params.extend([value, value])
            rows = await ctx.db.fetch_all(
                f"SELECT id FROM memory_claims WHERE subject_id = ? AND claim_type_key = ? AND status = 'active'{extra}",
                tuple(params),
            )
            if not rows:
                return json.dumps({"error": f"No matching active claim found"})
            for r in rows:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (r["id"],),
                )
            # Write truth claim
            truth_claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=f"[removed {claim_type_key}] {reason}",
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, truth_claim)
            return json.dumps({
                "ok": True,
                "action": "remove_claim",
                "entity_id": entity_id,
                "claims_removed": len(rows),
            })

        elif action == "set_truth":
            if not entity_id or not value:
                return json.dumps({"error": "entity_id and value required for set_truth"})
            claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=value,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, claim)
            return json.dumps({
                "ok": True,
                "action": "set_truth",
                "entity_id": entity_id,
                "claim_id": claim.id,
            })

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use remove_entity, remove_claim, or set_truth."})

    return [recall, find, note, memory_write, memory_read, memory_correct]
