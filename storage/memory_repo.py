import aiosqlite
from typing import List, Optional
from core.models import MemoryEntry, MemoryState


class MemoryRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_by_subject(self, subject_id: str, layer: Optional[str] = None) -> List[MemoryEntry]:
        if layer:
            async with self.db.execute(
                "SELECT * FROM memories WHERE subject_id = ? AND layer = ? ORDER BY updated_at DESC",
                (subject_id, layer),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM memories WHERE subject_id = ? ORDER BY layer, updated_at DESC",
                (subject_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def get_state(self, subject_id: str) -> MemoryState:
        entries = await self.get_by_subject(subject_id)
        state = MemoryState()
        for e in entries:
            if e.layer == "important":
                state.important.append(e)
            elif e.layer == "general":
                state.general.append(e)
            elif e.layer == "fleeting":
                state.fleeting.append(e)
        return state

    async def upsert(self, entry: MemoryEntry) -> None:
        await self.db.execute(
            """
            INSERT INTO memories (memory_id, content, layer, category, importance, subject_id,
                                  created_at, updated_at, expires_at, source, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                content = excluded.content,
                layer = excluded.layer,
                category = excluded.category,
                importance = excluded.importance,
                subject_id = excluded.subject_id,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                source = excluded.source,
                confidence = excluded.confidence
            """,
            (
                entry.memory_id,
                entry.content,
                entry.layer,
                entry.category,
                entry.importance,
                entry.subject_id,
                entry.created_at,
                entry.updated_at,
                entry.expires_at,
                entry.source,
                entry.confidence,
            ),
        )
        await self.db.commit()

    async def delete(self, memory_id: str) -> bool:
        cursor = await self.db.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def replace_state(self, subject_id: str, state: MemoryState) -> None:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            await self.db.execute("DELETE FROM memories WHERE subject_id = ?", (subject_id,))
            for entry in state.all_entries():
                entry.subject_id = subject_id
                await self._upsert_no_commit(entry)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def replace_layer(self, subject_id: str, layer: str, entries: List[MemoryEntry]) -> None:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            await self.db.execute(
                "DELETE FROM memories WHERE subject_id = ? AND layer = ?",
                (subject_id, layer),
            )
            for entry in entries:
                entry.subject_id = subject_id
                entry.layer = layer
                await self._upsert_no_commit(entry)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def _upsert_no_commit(self, entry: MemoryEntry) -> None:
        await self.db.execute(
            """
            INSERT INTO memories (memory_id, content, layer, category, importance, subject_id,
                                  created_at, updated_at, expires_at, source, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                content = excluded.content,
                layer = excluded.layer,
                category = excluded.category,
                importance = excluded.importance,
                subject_id = excluded.subject_id,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                source = excluded.source,
                confidence = excluded.confidence
            """,
            (
                entry.memory_id,
                entry.content,
                entry.layer,
                entry.category,
                entry.importance,
                entry.subject_id,
                entry.created_at,
                entry.updated_at,
                entry.expires_at,
                entry.source,
                entry.confidence,
            ),
        )

    async def list_all_subjects(self) -> List[str]:
        async with self.db.execute(
            "SELECT DISTINCT subject_id FROM memories"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["subject_id"] for row in rows]

    async def delete_by_subject(self, subject_id: str) -> None:
        await self.db.execute("DELETE FROM memories WHERE subject_id = ?", (subject_id,))
        await self.db.commit()

    async def count_by_subject_layer(self, subject_id: str, layer: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE subject_id = ? AND layer = ?",
            (subject_id, layer),
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    def _row_to_entry(self, row: aiosqlite.Row) -> MemoryEntry:
        return MemoryEntry(
            memory_id=row["memory_id"],
            content=row["content"],
            layer=row["layer"],
            category=row["category"],
            importance=row["importance"],
            subject_id=row["subject_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            source=row["source"],
            confidence=row["confidence"],
        )
