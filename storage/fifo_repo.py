if __package__ and "." in __package__:
    from ..core.models import ConversationTurn
    from .database import SQLiteDB
else:
    from core.models import ConversationTurn
    from storage.database import SQLiteDB


class FifoRepository:
    def __init__(self, db: SQLiteDB):
        self.database, self.db = db, db.conn

    async def get_turns(self, user_id: str, limit: int, context_id: str = None):
        sql, params = "SELECT * FROM fifo_buffer WHERE user_id=?", [user_id]
        if context_id is not None:
            sql, params = sql + " AND context_id=?", [user_id, context_id]
        async with self.db.execute(
            sql + " ORDER BY id ASC LIMIT ?", params + [limit]
        ) as cursor:
            return [self._row(r) for r in await cursor.fetchall()]

    async def append_turn(self, user_id: str, turn: ConversationTurn):
        async with self.database.transaction():
            await self.db.execute(
                """INSERT INTO fifo_buffer(user_id,context_id,turn_id,user_message,assistant_message,timestamp,group_id)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO NOTHING""",
                (
                    user_id,
                    turn.context_id or "",
                    turn.turn_id,
                    turn.user_message,
                    turn.assistant_message,
                    turn.timestamp,
                    turn.group_id,
                ),
            )

    async def clear(self, user_id: str, context_id: str = None):
        async with self.database.transaction():
            if context_id is None:
                await self.db.execute(
                    "DELETE FROM fifo_buffer WHERE user_id=?", (user_id,)
                )
            else:
                await self.db.execute(
                    "DELETE FROM fifo_buffer WHERE user_id=? AND context_id=?",
                    (user_id, context_id),
                )

    async def count(self, user_id: str, context_id: str = None):
        sql, params = "SELECT COUNT(*) cnt FROM fifo_buffer WHERE user_id=?", [user_id]
        if context_id is not None:
            sql, params = sql + " AND context_id=?", [user_id, context_id]
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_expired_streams(self, cutoff: str):
        """返回最老一轮早于 cutoff 的 user/context 队列。"""
        async with self.db.execute(
            """SELECT user_id, context_id, MIN(timestamp) AS oldest_at, COUNT(*) AS turn_count
            FROM fifo_buffer GROUP BY user_id, context_id HAVING MIN(timestamp) <= ?""",
            (cutoff,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def delete_oldest(self, user_id: str, keep: int, context_id: str = None):
        async with self.database.transaction():
            if context_id is None:
                await self.db.execute(
                    """DELETE FROM fifo_buffer WHERE user_id=? AND id NOT IN
                    (SELECT id FROM fifo_buffer WHERE user_id=? ORDER BY id DESC LIMIT ?)""",
                    (user_id, user_id, keep),
                )
            else:
                await self.db.execute(
                    """DELETE FROM fifo_buffer WHERE user_id=? AND context_id=? AND id NOT IN
                    (SELECT id FROM fifo_buffer WHERE user_id=? AND context_id=? ORDER BY id DESC LIMIT ?)""",
                    (user_id, context_id, user_id, context_id, keep),
                )

    def _row(self, row):
        return ConversationTurn(
            turn_id=row["turn_id"],
            user_id=row["user_id"],
            user_message=row["user_message"],
            assistant_message=row["assistant_message"],
            timestamp=row["timestamp"],
            context_id=row["context_id"],
            group_id=row["group_id"],
        )
