from core.models import GroupObservation
from storage.database import SQLiteDB


class GroupObservationRepository:
    def __init__(self, db: SQLiteDB):
        self.database, self.db = db, db.conn

    async def append(self, observation: GroupObservation) -> bool:
        async with self.database.transaction():
            cursor = await self.db.execute(
                """INSERT INTO group_observation_buffer
                (observation_id,context_id,group_id,sender_user_id,sender_name,content,timestamp)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO NOTHING""",
                (
                    observation.observation_id,
                    observation.context_id,
                    observation.group_id,
                    observation.sender_user_id,
                    observation.sender_name,
                    observation.content,
                    observation.timestamp,
                ),
            )
            return cursor.rowcount > 0

    async def get(self, context_id: str, limit: int = 100):
        async with self.db.execute(
            """SELECT * FROM group_observation_buffer WHERE context_id=?
            ORDER BY id ASC LIMIT ?""",
            (context_id, limit),
        ) as cursor:
            return [self._row(row) for row in await cursor.fetchall()]

    async def get_recent(self, context_id: str, limit: int = 12):
        async with self.db.execute(
            """SELECT * FROM (SELECT * FROM group_observation_buffer
            WHERE context_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC""",
            (context_id, limit),
        ) as cursor:
            return [self._row(row) for row in await cursor.fetchall()]

    async def count(self, context_id: str = "") -> int:
        sql, params = "SELECT COUNT(*) count FROM group_observation_buffer", []
        if context_id:
            sql += " WHERE context_id=?"
            params.append(context_id)
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row["count"] if row else 0

    async def get_expired_streams(self, cutoff: str):
        async with self.db.execute(
            """SELECT context_id,group_id,MIN(timestamp) oldest_at,COUNT(*) message_count
            FROM group_observation_buffer GROUP BY context_id,group_id
            HAVING MIN(timestamp) <= ?""",
            (cutoff,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def trim(self, context_id: str, keep: int):
        async with self.database.transaction():
            await self.db.execute(
                """DELETE FROM group_observation_buffer WHERE context_id=? AND id NOT IN
                (SELECT id FROM group_observation_buffer WHERE context_id=?
                 ORDER BY id DESC LIMIT ?)""",
                (context_id, context_id, keep),
            )

    async def clear_ids_no_commit(self, observation_ids):
        ids = list(dict.fromkeys(observation_ids))
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        await self.db.execute(
            f"DELETE FROM group_observation_buffer WHERE observation_id IN ({marks})",
            ids,
        )

    async def clear_context(self, context_id: str):
        async with self.database.transaction():
            await self.db.execute(
                "DELETE FROM group_observation_buffer WHERE context_id=?", (context_id,)
            )

    def _row(self, row):
        return GroupObservation(
            observation_id=row["observation_id"],
            context_id=row["context_id"],
            group_id=row["group_id"],
            sender_user_id=row["sender_user_id"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
        )
