import aiosqlite
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager


class SQLiteDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> "SQLiteDB":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        return self

    async def init_tables(self) -> None:
        if not self._conn:
            raise RuntimeError("Database not connected. Call connect() first.")

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                layer TEXT NOT NULL CHECK(layer IN ('important', 'general', 'fleeting')),
                category TEXT NOT NULL CHECK(category IN ('profile', 'preference', 'task', 'fact', 'event')),
                importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
                subject_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                source TEXT NOT NULL CHECK(source IN ('auto_summary', 'manual', 'tool_call')),
                confidence INTEGER CHECK(confidence BETWEEN 1 AND 5)
            );

            CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject_id);
            CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories(layer);
            CREATE INDEX IF NOT EXISTS idx_memories_subject_layer ON memories(subject_id, layer);

            CREATE TABLE IF NOT EXISTS fifo_buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT NOT NULL,
                turn_id TEXT NOT NULL UNIQUE,
                user_message TEXT NOT NULL,
                assistant_message TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                group_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_fifo_subject ON fifo_buffer(subject_id);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        await self._conn.commit()

    @asynccontextmanager
    async def transaction(self):
        if not self._conn:
            raise RuntimeError("Database not connected.")
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            yield
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected.")
        return self._conn

    async def vacuum_backup(self, backup_path: Path) -> None:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        await self._conn.execute("VACUUM INTO ?", (str(backup_path),))
