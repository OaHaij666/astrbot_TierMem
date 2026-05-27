import aiosqlite
from typing import List
from core.models import ConversationTurn


class FifoRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_turns(self, subject_id: str, limit: int) -> List[ConversationTurn]:
        async with self.db.execute(
            "SELECT * FROM fifo_buffer WHERE subject_id = ? ORDER BY id ASC LIMIT ?",
            (subject_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_turn(row) for row in rows]

    async def append_turn(self, subject_id: str, turn: ConversationTurn) -> None:
        await self.db.execute(
            """
            INSERT INTO fifo_buffer (subject_id, turn_id, user_message, assistant_message, timestamp, group_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                user_message = excluded.user_message,
                assistant_message = excluded.assistant_message
            """,
            (
                subject_id,
                turn.turn_id,
                turn.user_message,
                turn.assistant_message,
                turn.timestamp,
                turn.group_id,
            ),
        )
        await self.db.commit()

    async def clear(self, subject_id: str) -> None:
        await self.db.execute("DELETE FROM fifo_buffer WHERE subject_id = ?", (subject_id,))
        await self.db.commit()

    async def count(self, subject_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM fifo_buffer WHERE subject_id = ?", (subject_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def delete_oldest(self, subject_id: str, keep: int) -> None:
        """只保留最新的 keep 条，删除旧的"""
        await self.db.execute(
            """
            DELETE FROM fifo_buffer WHERE subject_id = ? AND id NOT IN (
                SELECT id FROM fifo_buffer WHERE subject_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (subject_id, subject_id, keep),
        )
        await self.db.commit()

    async def get_active_subjects(self, group_id: str, count: int, exclude_user_id: str = "") -> List[str]:
        if exclude_user_id:
            async with self.db.execute(
                """
                SELECT DISTINCT subject_id FROM fifo_buffer
                WHERE group_id = ? AND subject_id NOT LIKE ?
                ORDER BY id DESC LIMIT ?
                """,
                (group_id, f"{exclude_user_id}#%", count),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                """
                SELECT DISTINCT subject_id FROM fifo_buffer
                WHERE group_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (group_id, count),
            ) as cursor:
                rows = await cursor.fetchall()
        return [row["subject_id"] for row in rows]

    def _row_to_turn(self, row: aiosqlite.Row) -> ConversationTurn:
        return ConversationTurn(
            turn_id=row["turn_id"],
            user_message=row["user_message"],
            assistant_message=row["assistant_message"],
            timestamp=row["timestamp"],
            group_id=row["group_id"],
        )
