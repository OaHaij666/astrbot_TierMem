import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import aiosqlite


SCHEMA_VERSION = "4"


class SQLiteDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> "SQLiteDB":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        return self

    async def init_tables(self) -> None:
        current = None
        try:
            async with self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ) as cursor:
                row = await cursor.fetchone()
                current = row["value"] if row else None
        except aiosqlite.OperationalError:
            pass
        if current != SCHEMA_VERSION:
            await self.conn.executescript(
                """DROP TRIGGER IF EXISTS memories_ai; DROP TRIGGER IF EXISTS memories_ad;
                DROP TRIGGER IF EXISTS memories_au; DROP TABLE IF EXISTS memories_fts;
                DROP TABLE IF EXISTS memory_entity_mentions;
                DROP TABLE IF EXISTS relation_evidence; DROP TABLE IF EXISTS relations;
                DROP TABLE IF EXISTS group_observation_buffer;
                DROP TABLE IF EXISTS entities; DROP TABLE IF EXISTS fifo_buffer;
                DROP TABLE IF EXISTS memories; DROP TABLE IF EXISTS meta;"""
            )
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
              memory_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, content TEXT NOT NULL,
              normalized_content TEXT NOT NULL,
              layer TEXT NOT NULL CHECK(layer IN ('core','semantic','episodic','working')),
              category TEXT NOT NULL CHECK(category IN ('profile','preference','task','fact','event','relation')),
              importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
              confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
              strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1),
              stability REAL NOT NULL CHECK(stability BETWEEN 0 AND 1),
              decay_rate REAL NOT NULL CHECK(decay_rate >= 0), created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, last_accessed_at TEXT, last_confirmed_at TEXT NOT NULL,
              confirmation_count INTEGER NOT NULL DEFAULT 1, expires_at TEXT, source TEXT NOT NULL,
              source_turn_id TEXT, visibility_scope TEXT NOT NULL DEFAULT 'private',
              context_id TEXT,
              status TEXT NOT NULL DEFAULT 'active');
            CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner_user_id,status);
            CREATE INDEX IF NOT EXISTS idx_memories_owner_layer ON memories(owner_user_id,layer,status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedupe
              ON memories(owner_user_id,layer,normalized_content,visibility_scope,IFNULL(context_id,''))
              WHERE status='active';

            CREATE TABLE IF NOT EXISTS entities (
              entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, name TEXT NOT NULL,
              aliases_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS relations (
              relation_id TEXT PRIMARY KEY,
              source_entity_id TEXT NOT NULL REFERENCES entities(entity_id), relation_type TEXT NOT NULL,
              target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
              confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
              strength REAL NOT NULL CHECK(strength BETWEEN 0 AND 1),
              stability REAL NOT NULL CHECK(stability BETWEEN 0 AND 1),
              decay_rate REAL NOT NULL CHECK(decay_rate >= 0), created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, last_confirmed_at TEXT NOT NULL,
              confirmation_count INTEGER NOT NULL DEFAULT 1, valid_from TEXT, valid_until TEXT,
              status TEXT NOT NULL DEFAULT 'active', visibility_scope TEXT NOT NULL DEFAULT 'private',
              context_id TEXT, owner_user_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_entity_id,status);
            CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_entity_id,status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_dedupe
              ON relations(owner_user_id,source_entity_id,relation_type,target_entity_id,IFNULL(context_id,''))
              WHERE status='active';
            CREATE TABLE IF NOT EXISTS relation_evidence (
              evidence_id TEXT PRIMARY KEY,
              relation_id TEXT NOT NULL REFERENCES relations(relation_id) ON DELETE CASCADE,
              excerpt TEXT NOT NULL, speaker_user_id TEXT, turn_id TEXT, created_at TEXT NOT NULL,
              memory_id TEXT REFERENCES memories(memory_id) ON DELETE SET NULL,
              polarity TEXT NOT NULL DEFAULT 'support' CHECK(polarity IN ('support','refute')),
              evidence_weight REAL NOT NULL DEFAULT 1.0 CHECK(evidence_weight BETWEEN 0 AND 1));
            CREATE INDEX IF NOT EXISTS idx_evidence_relation ON relation_evidence(relation_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_memory ON relation_evidence(memory_id);

            CREATE TABLE IF NOT EXISTS memory_entity_mentions (
              memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
              entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
              mention_role TEXT NOT NULL DEFAULT 'mention',
              confidence REAL NOT NULL DEFAULT 0.8 CHECK(confidence BETWEEN 0 AND 1),
              PRIMARY KEY(memory_id,entity_id));
            CREATE INDEX IF NOT EXISTS idx_mentions_entity ON memory_entity_mentions(entity_id);

            CREATE TABLE IF NOT EXISTS fifo_buffer (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, context_id TEXT NOT NULL,
              turn_id TEXT NOT NULL UNIQUE, user_message TEXT NOT NULL, assistant_message TEXT NOT NULL,
              timestamp TEXT NOT NULL, group_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_fifo_user ON fifo_buffer(user_id,id);
            CREATE INDEX IF NOT EXISTS idx_fifo_context ON fifo_buffer(context_id,id);

            CREATE TABLE IF NOT EXISTS group_observation_buffer (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              observation_id TEXT NOT NULL UNIQUE,
              context_id TEXT NOT NULL,
              group_id TEXT NOT NULL,
              sender_user_id TEXT NOT NULL,
              sender_name TEXT NOT NULL,
              content TEXT NOT NULL,
              timestamp TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_group_observation_context
              ON group_observation_buffer(context_id,id);
            CREATE INDEX IF NOT EXISTS idx_group_observation_oldest
              ON group_observation_buffer(timestamp);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO meta(key,value) VALUES('schema_version','4')
              ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """
        )
        await self._init_fts()
        await self.conn.commit()

    async def _init_fts(self) -> None:
        try:
            await self.conn.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                memory_id UNINDEXED, content, content='memories', content_rowid='rowid',
                tokenize='trigram')"""
            )
            await self.conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                  INSERT INTO memories_fts(rowid,memory_id,content)
                  VALUES(new.rowid,new.memory_id,new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts,rowid,memory_id,content)
                  VALUES('delete',old.rowid,old.memory_id,old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts,rowid,memory_id,content)
                  VALUES('delete',old.rowid,old.memory_id,old.content);
                  INSERT INTO memories_fts(rowid,memory_id,content)
                  VALUES(new.rowid,new.memory_id,new.content);
                END;
                """
            )
            await self.conn.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"
            )
            available, tokenizer = "1", "trigram"
        except aiosqlite.OperationalError:
            await self.conn.executescript(
                """DROP TRIGGER IF EXISTS memories_ai; DROP TRIGGER IF EXISTS memories_ad;
                DROP TRIGGER IF EXISTS memories_au; DROP TABLE IF EXISTS memories_fts;"""
            )
            available, tokenizer = "0", "like"
        await self.conn.executemany(
            """INSERT INTO meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (("fts_available", available), ("fts_tokenizer", tokenizer)),
        )

    async def fts_status(self) -> dict:
        result = {"available": False, "tokenizer": "like"}
        async with self.conn.execute(
            "SELECT key,value FROM meta WHERE key IN ('fts_available','fts_tokenizer')"
        ) as cursor:
            for row in await cursor.fetchall():
                if row["key"] == "fts_available":
                    result["available"] = row["value"] == "1"
                elif row["key"] == "fts_tokenizer":
                    result["tokenizer"] = row["value"]
        return result

    @asynccontextmanager
    async def transaction(self):
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    async def vacuum_backup(self, backup_path: Path) -> None:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._write_lock:
            await self.conn.execute("VACUUM INTO ?", (str(backup_path),))
            await self.conn.commit()
