import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import aiosqlite

from core.models import MemoryEntry, MemoryState, utc_now
from storage.database import SQLiteDB


def normalize_content(content: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", content).strip().casefold().split())


_LATIN_TERM = re.compile(r"[a-z0-9][a-z0-9_.+-]*", re.IGNORECASE)
_HAN_BLOCK = re.compile(r"[\u3400-\u9fff]+")


@dataclass
class ScoredMemory:
    memory: MemoryEntry
    search_mode: str
    rank_position: int
    text_score: float
    strength_score: float
    importance_score: float
    confidence_score: float
    score: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class AtomSearchResult:
    query: str
    mode: str
    query_terms: List[str] = field(default_factory=list)
    hits: List[ScoredMemory] = field(default_factory=list)
    fts_available: bool = False
    tokenizer: str = "like"

    @property
    def memories(self) -> List[MemoryEntry]:
        return [hit.memory for hit in self.hits]


class MemoryRepository:
    def __init__(self, db: SQLiteDB):
        self.database, self.db = db, db.conn

    async def get_by_user(
        self, user_id: str, layer: Optional[str] = None
    ) -> List[MemoryEntry]:
        sql, params = (
            "SELECT * FROM memories WHERE owner_user_id=? AND status='active'",
            [user_id],
        )
        if layer:
            sql, params = sql + " AND layer=?", [user_id, layer]
        async with self.db.execute(sql + " ORDER BY updated_at DESC", params) as cursor:
            return [self._row_to_entry(r) for r in await cursor.fetchall()]

    async def get_by_subject(self, subject_id: str, layer: Optional[str] = None):
        return await self.get_by_user(subject_id, layer)

    async def get_state(self, user_id: str) -> MemoryState:
        state = MemoryState()
        for entry in await self.get_by_user(user_id):
            getattr(state, entry.layer).append(entry)
        return state

    async def retrieve(
        self, user_id: str, limit: int, min_strength: float
    ) -> List[MemoryEntry]:
        entries = [
            e
            for e in await self.get_by_user(user_id)
            if e.effective_strength() >= min_strength
        ]
        entries.sort(key=lambda e: (e.retrieval_score(), e.updated_at), reverse=True)
        return entries[:limit]

    async def search_atoms(
        self,
        query: str,
        user_id: str,
        limit: int = 12,
        min_strength: float = 0.0,
        categories: Optional[Sequence[str]] = None,
        fts_candidate_limit: int = 40,
        like_candidate_limit: int = 24,
        background_limit: int = 4,
        query_term_limit: int = 24,
        context_id: Optional[str] = None,
        additional_owner_ids: Optional[Sequence[str]] = None,
    ) -> AtomSearchResult:
        owner_ids = list(dict.fromkeys([user_id, *(additional_owner_ids or [])]))
        status = await self.database.fts_status()
        terms = self._query_terms(query, query_term_limit)
        rows = []
        mode = "fts5"
        if query.strip() and terms and status["available"]:
            try:
                rows = await self._search_fts(
                    terms, owner_ids, categories, fts_candidate_limit, context_id
                )
            except aiosqlite.OperationalError:
                rows = []
        if not rows and query.strip():
            mode = "like"
            like_terms = terms or self._like_terms(query, query_term_limit)
            rows = await self._search_like(
                like_terms, owner_ids, categories, like_candidate_limit, context_id
            )
        if not rows:
            mode = "background"
            rows = await self._search_background(
                owner_ids,
                categories,
                max(1, min(background_limit, limit)),
                context_id,
            )

        hits = []
        for position, row in enumerate(rows):
            memory = self._row_to_entry(row)
            strength = memory.effective_strength()
            if strength < min_strength:
                continue
            if mode == "fts5":
                text_score = 1.0 / (1.0 + position)
            elif mode == "like":
                text_score = 0.6 / (1.0 + position)
            else:
                text_score = 0.0
            importance = memory.importance / 5.0
            score = (
                0.50 * text_score
                + 0.20 * strength
                + 0.20 * importance
                + 0.10 * memory.confidence
            )
            reasons = [
                "FTS5 trigram 文本命中"
                if mode == "fts5"
                else "LIKE 子串降级命中"
                if mode == "like"
                else "无关键词命中，使用重要记忆兜底"
            ]
            hits.append(
                ScoredMemory(
                    memory,
                    mode,
                    position,
                    round(text_score, 4),
                    round(strength, 4),
                    round(importance, 4),
                    round(memory.confidence, 4),
                    round(score, 4),
                    reasons,
                )
            )
        hits.sort(key=lambda item: (item.score, item.memory.updated_at), reverse=True)
        return AtomSearchResult(
            query=query,
            mode=mode,
            query_terms=terms,
            hits=hits[:limit],
            fts_available=status["available"],
            tokenizer=status["tokenizer"],
        )

    def _query_terms(self, query: str, limit: int) -> List[str]:
        normalized = normalize_content(query)
        terms = [term for term in _LATIN_TERM.findall(normalized) if len(term) >= 3]
        for block in _HAN_BLOCK.findall(normalized):
            if len(block) < 3:
                continue
            for width in (3, 4, 5):
                if len(block) < width:
                    continue
                terms.extend(
                    block[i : i + width] for i in range(len(block) - width + 1)
                )
        return list(dict.fromkeys(terms))[: max(1, limit)]

    def _like_terms(self, query: str, limit: int) -> List[str]:
        normalized = normalize_content(query)
        terms = _LATIN_TERM.findall(normalized)
        for block in _HAN_BLOCK.findall(normalized):
            if len(block) <= 5:
                terms.append(block)
            else:
                terms.extend(block[i : i + 2] for i in range(len(block) - 1))
        return list(dict.fromkeys(term for term in terms if term))[: max(1, limit)]

    def _scope_clause(self, categories: Optional[Sequence[str]]):
        if not categories:
            return "", []
        values = list(dict.fromkeys(categories))
        return f" AND m.category IN ({','.join('?' for _ in values)})", values

    def _visibility_clause(self, context_id: Optional[str]):
        if context_id is None:
            return "", []
        return (
            " AND (m.visibility_scope IN ('private','public') OR "
            "(m.visibility_scope='group' AND m.context_id=?))",
            [context_id],
        )

    def _owner_clause(self, owner_ids):
        values = list(dict.fromkeys(owner_ids))
        return f"m.owner_user_id IN ({','.join('?' for _ in values)})", values

    async def _search_fts(self, terms, owner_ids, categories, limit, context_id):
        category_sql, category_params = self._scope_clause(categories)
        visibility_sql, visibility_params = self._visibility_clause(context_id)
        owner_sql, owner_params = self._owner_clause(owner_ids)
        expression = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        sql = (
            "SELECT m.*,bm25(memories_fts) AS raw_rank FROM memories_fts "
            "JOIN memories m ON m.rowid=memories_fts.rowid "
            f"WHERE memories_fts MATCH ? AND {owner_sql} AND m.status='active'"
            + category_sql
            + visibility_sql
            + " ORDER BY bm25(memories_fts) ASC LIMIT ?"
        )
        async with self.db.execute(
            sql,
            [expression, *owner_params, *category_params, *visibility_params, limit],
        ) as cursor:
            return await cursor.fetchall()

    async def _search_like(self, terms, owner_ids, categories, limit, context_id):
        if not terms:
            return []
        category_sql, category_params = self._scope_clause(categories)
        visibility_sql, visibility_params = self._visibility_clause(context_id)
        owner_sql, owner_params = self._owner_clause(owner_ids)
        likes = " OR ".join("m.normalized_content LIKE ? ESCAPE '\\'" for _ in terms)
        patterns = [
            "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            for term in terms
        ]
        sql = (
            f"SELECT m.* FROM memories m WHERE {owner_sql} AND m.status='active'"
            + category_sql
            + visibility_sql
            + f" AND ({likes}) ORDER BY m.updated_at DESC LIMIT ?"
        )
        async with self.db.execute(
            sql,
            [*owner_params, *category_params, *visibility_params, *patterns, limit],
        ) as cursor:
            return await cursor.fetchall()

    async def _search_background(self, owner_ids, categories, limit, context_id):
        category_sql, category_params = self._scope_clause(categories)
        visibility_sql, visibility_params = self._visibility_clause(context_id)
        owner_sql, owner_params = self._owner_clause(owner_ids)
        sql = (
            f"SELECT m.* FROM memories m WHERE {owner_sql} AND m.status='active'"
            + category_sql
            + visibility_sql
            + " ORDER BY m.importance DESC,m.confidence DESC,m.updated_at DESC LIMIT ?"
        )
        async with self.db.execute(
            sql, [*owner_params, *category_params, *visibility_params, limit]
        ) as cursor:
            return await cursor.fetchall()

    async def get(self, memory_id: str, owner_user_id: Optional[str] = None):
        sql, params = (
            "SELECT * FROM memories WHERE memory_id=? AND status='active'",
            [memory_id],
        )
        if owner_user_id is not None:
            sql, params = sql + " AND owner_user_id=?", [memory_id, owner_user_id]
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return self._row_to_entry(row) if row else None

    async def upsert(self, entry: MemoryEntry) -> MemoryEntry:
        async with self.database.transaction():
            return await self.upsert_no_commit(entry)

    async def upsert_no_commit(self, entry: MemoryEntry) -> MemoryEntry:
        normalized = normalize_content(entry.content)
        async with self.db.execute(
            """SELECT * FROM memories WHERE owner_user_id=? AND layer=?
            AND normalized_content=? AND visibility_scope=?
            AND IFNULL(context_id,'')=IFNULL(?,'') AND status='active'""",
            (
                entry.owner_user_id,
                entry.layer,
                normalized,
                entry.visibility_scope,
                entry.context_id,
            ),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row["memory_id"] != entry.memory_id:
            old = self._row_to_entry(row)
            old.confirmation_count += 1
            old.confidence = min(1.0, max(old.confidence, entry.confidence) + 0.04)
            old.strength = min(
                1.0, max(old.effective_strength(), entry.strength) + 0.12
            )
            old.stability = min(1.0, max(old.stability, entry.stability) + 0.05)
            old.last_confirmed_at = old.updated_at = utc_now()
            entry = old
        await self.db.execute(
            """INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(memory_id) DO UPDATE SET content=excluded.content,
            normalized_content=excluded.normalized_content,layer=excluded.layer,category=excluded.category,
            importance=excluded.importance,confidence=excluded.confidence,strength=excluded.strength,
            stability=excluded.stability,decay_rate=excluded.decay_rate,updated_at=excluded.updated_at,
            last_accessed_at=excluded.last_accessed_at,last_confirmed_at=excluded.last_confirmed_at,
            confirmation_count=excluded.confirmation_count,expires_at=excluded.expires_at,
            source=excluded.source,source_turn_id=excluded.source_turn_id,
            visibility_scope=excluded.visibility_scope,context_id=excluded.context_id,
            status=excluded.status""",
            self._values(entry, normalized),
        )
        return entry

    async def deactivate_no_commit(self, memory_id: str, owner_user_id: str) -> bool:
        cur = await self.db.execute(
            "UPDATE memories SET status='superseded',updated_at=? WHERE memory_id=? AND owner_user_id=? AND status='active'",
            (utc_now(), memory_id, owner_user_id),
        )
        return cur.rowcount > 0

    async def delete(self, memory_id: str, owner_user_id: Optional[str] = None) -> bool:
        if owner_user_id is None:
            return False
        async with self.database.transaction():
            return await self.deactivate_no_commit(memory_id, owner_user_id)

    async def delete_by_user(self, user_id: str):
        async with self.database.transaction():
            await self.db.execute(
                "DELETE FROM memories WHERE owner_user_id=?", (user_id,)
            )

    async def delete_by_subject(self, subject_id: str):
        await self.delete_by_user(subject_id)

    async def count_by_user_layer(self, user_id: str, layer: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) cnt FROM memories WHERE owner_user_id=? AND layer=? AND status='active'",
            (user_id, layer),
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def count_by_subject_layer(self, subject_id: str, layer: str):
        return await self.count_by_user_layer(subject_id, layer)

    async def list_all_users(self):
        async with self.db.execute(
            "SELECT DISTINCT owner_user_id FROM memories"
        ) as cursor:
            return [r["owner_user_id"] for r in await cursor.fetchall()]

    async def list_recent(self, limit: int = 200, user_id: str = "", layer: str = ""):
        sql, params = "SELECT * FROM memories WHERE status='active'", []
        if user_id:
            sql += " AND owner_user_id=?"
            params.append(user_id)
        if layer:
            sql += " AND layer=?"
            params.append(layer)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(sql, params) as cursor:
            return [self._row_to_entry(r) for r in await cursor.fetchall()]

    async def list_all_subjects(self):
        return await self.list_all_users()

    async def prune(self, user_id: str, max_count: int) -> int:
        entries = await self.get_by_user(user_id)
        if len(entries) <= max_count:
            return 0
        entries.sort(key=lambda e: (e.retrieval_score(), e.updated_at))
        victims = entries[: len(entries) - max_count]
        async with self.database.transaction():
            for e in victims:
                await self.db.execute(
                    "UPDATE memories SET status='archived' WHERE memory_id=?",
                    (e.memory_id,),
                )
        return len(victims)

    def _values(self, e, normalized):
        return (
            e.memory_id,
            e.owner_user_id,
            e.content,
            normalized,
            e.layer,
            e.category,
            e.importance,
            e.confidence,
            e.strength,
            e.stability,
            e.decay_rate,
            e.created_at,
            e.updated_at,
            e.last_accessed_at,
            e.last_confirmed_at,
            e.confirmation_count,
            e.expires_at,
            e.source,
            e.source_turn_id,
            e.visibility_scope,
            e.context_id,
            e.status,
        )

    def _row_to_entry(self, row):
        return MemoryEntry(**{k: row[k] for k in MemoryEntry.__dataclass_fields__})
