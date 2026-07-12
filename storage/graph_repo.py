import json
from typing import Optional

from core.models import Entity, Relation, RelationEvidence, utc_now
from storage.database import SQLiteDB


class GraphRepository:
    def __init__(self, db: SQLiteDB):
        self.database, self.db = db, db.conn

    async def upsert_entity_no_commit(self, e: Entity):
        existing = await self.get_entity(e.entity_id)
        if existing:
            aliases = set(existing.aliases) | set(e.aliases)
            if existing.name and existing.name != e.name:
                aliases.add(existing.name)
            aliases.discard(e.name)
            e.aliases = sorted(aliases)
            e.created_at = existing.created_at
            e.metadata = {**existing.metadata, **e.metadata}
        await self.db.execute(
            """INSERT INTO entities VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET
            entity_type=excluded.entity_type,name=excluded.name,aliases_json=excluded.aliases_json,
            metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                e.entity_id,
                e.entity_type,
                e.name,
                json.dumps(e.aliases, ensure_ascii=False),
                json.dumps(e.metadata, ensure_ascii=False),
                e.created_at,
                e.updated_at,
            ),
        )
        return e

    async def list_entities(self, limit: int = 5000):
        async with self.db.execute(
            "SELECT * FROM entities ORDER BY updated_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            Entity(
                r["entity_id"],
                r["entity_type"],
                r["name"],
                json.loads(r["aliases_json"]),
                json.loads(r["metadata_json"]),
                r["created_at"],
                r["updated_at"],
            )
            for r in rows
        ]

    async def link_memory_entities_no_commit(
        self,
        memory_id: str,
        entity_ids,
        mention_role: str = "mention",
        confidence: float = 0.8,
    ):
        for entity_id in dict.fromkeys(entity_ids or []):
            await self.db.execute(
                """INSERT INTO memory_entity_mentions(memory_id,entity_id,mention_role,confidence)
                SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM entities WHERE entity_id=?)
                ON CONFLICT(memory_id,entity_id) DO UPDATE SET
                mention_role=excluded.mention_role,confidence=MAX(confidence,excluded.confidence)""",
                (memory_id, entity_id, mention_role, confidence, entity_id),
            )

    async def get_entities_for_memories(self, memory_ids):
        memory_ids = list(dict.fromkeys(memory_ids or []))
        if not memory_ids:
            return {}
        marks = ",".join("?" for _ in memory_ids)
        async with self.db.execute(
            f"""SELECT mem.memory_id,e.* FROM memory_entity_mentions mem
            JOIN entities e ON e.entity_id=mem.entity_id
            WHERE mem.memory_id IN ({marks}) ORDER BY mem.confidence DESC""",
            memory_ids,
        ) as cursor:
            rows = await cursor.fetchall()
        result = {memory_id: [] for memory_id in memory_ids}
        for row in rows:
            result.setdefault(row["memory_id"], []).append(self._entity_row(row))
        return result

    async def get_relations_touching(
        self,
        entity_ids,
        viewer_user_id: str,
        context_id: str,
        min_strength: float = 0.0,
        limit: int = 500,
    ):
        entity_ids = list(dict.fromkeys(entity_ids))
        if not entity_ids:
            return []
        marks = ",".join("?" for _ in entity_ids)
        async with self.db.execute(
            f"SELECT * FROM relations WHERE status='active' AND "
            f"(source_entity_id IN ({marks}) OR target_entity_id IN ({marks})) "
            "ORDER BY updated_at DESC LIMIT ?",
            entity_ids + entity_ids + [max(1, limit)],
        ) as cursor:
            relations = [self._row(r) for r in await cursor.fetchall()]
        return [
            relation
            for relation in relations
            if self._visible(relation, viewer_user_id, context_id)
            and relation.effective_strength() >= min_strength
        ]

    async def get_evidence_relations_for_memories(
        self, memory_ids, viewer_user_id: str, context_id: str
    ):
        memory_ids = list(dict.fromkeys(memory_ids or []))
        if not memory_ids:
            return []
        marks = ",".join("?" for _ in memory_ids)
        async with self.db.execute(
            f"""SELECT r.*,re.evidence_id AS ev_id,re.excerpt AS ev_excerpt,
            re.speaker_user_id AS ev_speaker_user_id,re.turn_id AS ev_turn_id,
            re.created_at AS ev_created_at,re.memory_id AS ev_memory_id,
            re.polarity AS ev_polarity,re.evidence_weight AS ev_weight
            FROM relation_evidence re JOIN relations r ON r.relation_id=re.relation_id
            WHERE re.memory_id IN ({marks}) AND r.status='active'""",
            memory_ids,
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            relation = self._row(row)
            if not self._visible(relation, viewer_user_id, context_id):
                continue
            evidence = RelationEvidence(
                evidence_id=row["ev_id"],
                relation_id=relation.relation_id,
                excerpt=row["ev_excerpt"],
                speaker_user_id=row["ev_speaker_user_id"] or "",
                turn_id=row["ev_turn_id"],
                created_at=row["ev_created_at"],
                memory_id=row["ev_memory_id"],
                polarity=row["ev_polarity"],
                evidence_weight=row["ev_weight"],
            )
            result.append((evidence.memory_id, relation, evidence))
        return result

    async def list_relations(self, limit: int = 500, status: str = "active"):
        async with self.db.execute(
            "SELECT * FROM relations WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ) as cursor:
            return [self._row(r) for r in await cursor.fetchall()]

    async def ensure_user(self, user_id: str, name: Optional[str] = None):
        async with self.database.transaction():
            return await self.upsert_entity_no_commit(
                Entity(f"user:{user_id}", "user", name or user_id)
            )

    async def get_entity(self, entity_id: str):
        async with self.db.execute(
            "SELECT * FROM entities WHERE entity_id=?", (entity_id,)
        ) as cursor:
            r = await cursor.fetchone()
        return (
            None
            if not r
            else Entity(
                r["entity_id"],
                r["entity_type"],
                r["name"],
                json.loads(r["aliases_json"]),
                json.loads(r["metadata_json"]),
                r["created_at"],
                r["updated_at"],
            )
        )

    async def upsert_relation_no_commit(
        self, relation: Relation, evidence: Optional[RelationEvidence] = None
    ):
        async with self.db.execute(
            """SELECT * FROM relations WHERE source_entity_id=? AND relation_type=? AND target_entity_id=?
            AND IFNULL(context_id,'')=IFNULL(?,'') AND owner_user_id=? AND status='active'""",
            (
                relation.source_entity_id,
                relation.relation_type,
                relation.target_entity_id,
                relation.context_id,
                relation.owner_user_id,
            ),
        ) as cur:
            old = await cur.fetchone()
        if old and old["relation_id"] != relation.relation_id:
            relation.relation_id, relation.created_at = (
                old["relation_id"],
                old["created_at"],
            )
            relation.confirmation_count = old["confirmation_count"] + 1
            relation.confidence = min(
                1.0, max(old["confidence"], relation.confidence) + 0.04
            )
            relation.strength = min(1.0, max(old["strength"], relation.strength) + 0.12)
            relation.stability = min(
                1.0, max(old["stability"], relation.stability) + 0.05
            )
            relation.last_confirmed_at = relation.updated_at = utc_now()
        await self.db.execute(
            """INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(relation_id) DO UPDATE SET relation_type=excluded.relation_type,
            confidence=excluded.confidence,strength=excluded.strength,stability=excluded.stability,
            decay_rate=excluded.decay_rate,updated_at=excluded.updated_at,
            last_confirmed_at=excluded.last_confirmed_at,confirmation_count=excluded.confirmation_count,
            valid_until=excluded.valid_until,status=excluded.status,
            visibility_scope=excluded.visibility_scope,context_id=excluded.context_id,
            owner_user_id=excluded.owner_user_id""",
            (
                relation.relation_id,
                relation.source_entity_id,
                relation.relation_type,
                relation.target_entity_id,
                relation.confidence,
                relation.strength,
                relation.stability,
                relation.decay_rate,
                relation.created_at,
                relation.updated_at,
                relation.last_confirmed_at,
                relation.confirmation_count,
                relation.valid_from,
                relation.valid_until,
                relation.status,
                relation.visibility_scope,
                relation.context_id,
                relation.owner_user_id,
            ),
        )
        if evidence and evidence.excerpt:
            evidence.relation_id = relation.relation_id
            await self.add_evidence_no_commit(evidence)
        return relation

    async def add_evidence_no_commit(self, evidence: RelationEvidence):
        await self.db.execute(
            """INSERT OR IGNORE INTO relation_evidence
            (evidence_id,relation_id,excerpt,speaker_user_id,turn_id,created_at,
             memory_id,polarity,evidence_weight) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                evidence.evidence_id,
                evidence.relation_id,
                evidence.excerpt,
                evidence.speaker_user_id,
                evidence.turn_id,
                evidence.created_at,
                evidence.memory_id,
                evidence.polarity,
                evidence.evidence_weight,
            ),
        )

    async def deactivate_no_commit(self, relation_id: str, anchor_entity_id: str):
        cur = await self.db.execute(
            """UPDATE relations SET status='superseded',updated_at=? WHERE relation_id=?
            AND (source_entity_id=? OR target_entity_id=?)""",
            (utc_now(), relation_id, anchor_entity_id, anchor_entity_id),
        )
        return cur.rowcount > 0

    async def get_neighbors(
        self, entity_id: str, limit: int, min_strength: float, context_id=None
    ):
        async with self.db.execute(
            "SELECT * FROM relations WHERE status='active' AND (source_entity_id=? OR target_entity_id=?)",
            (entity_id, entity_id),
        ) as cursor:
            relations = [self._row(r) for r in await cursor.fetchall()]
        relations = [
            r
            for r in relations
            if not (
                r.visibility_scope == "private"
                and r.owner_user_id
                and entity_id != f"user:{r.owner_user_id}"
            )
            and not (r.visibility_scope == "group" and r.context_id != context_id)
            and r.effective_strength() >= min_strength
        ]
        relations.sort(
            key=lambda r: (r.effective_strength() * r.confidence, r.updated_at),
            reverse=True,
        )
        return relations[:limit]

    async def clear_user_graph(self, user_id: str):
        entity_id = f"user:{user_id}"
        async with self.database.transaction():
            await self.db.execute(
                "DELETE FROM relations WHERE source_entity_id=? OR target_entity_id=?",
                (entity_id, entity_id),
            )
            await self.db.execute(
                "DELETE FROM entities WHERE entity_id=?", (entity_id,)
            )

    def _row(self, r):
        return Relation(**{k: r[k] for k in Relation.__dataclass_fields__})

    def _entity_row(self, r):
        return Entity(
            r["entity_id"],
            r["entity_type"],
            r["name"],
            json.loads(r["aliases_json"]),
            json.loads(r["metadata_json"]),
            r["created_at"],
            r["updated_at"],
        )

    def _visible(self, relation, viewer_user_id: str, context_id: str):
        if (
            relation.visibility_scope == "private"
            and relation.owner_user_id
            and relation.owner_user_id != viewer_user_id
        ):
            return False
        if relation.visibility_scope == "group" and relation.context_id != context_id:
            return False
        return True
