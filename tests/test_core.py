import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models import (
    ConversationTurn,
    Entity,
    MemoryEntry,
    Relation,
    RelationEvidence,
    decay_rate_from_half_life,
)
from core.config import PluginConfig
from service.graph_retriever import GraphRetriever
from storage.database import SQLiteDB
from storage.fifo_repo import FifoRepository
from storage.graph_repo import GraphRepository
from storage.memory_repo import MemoryRepository


class DecayTests(unittest.TestCase):
    def test_json_text_relation_keywords_are_parsed(self):
        config = PluginConfig.from_astrbot_config(
            {"relation_intent_keywords": '{"friend_of":["朋友"]}'}
        )
        self.assertEqual(config.relation_intent_keywords, {"friend_of": ["朋友"]})

    def test_half_life_reduces_strength(self):
        memory = MemoryEntry(
            "m1",
            "u1",
            "临时任务",
            layer="working",
            strength=1.0,
            stability=0.5,
            decay_rate=decay_rate_from_half_life(10),
            last_confirmed_at=(
                datetime.now(timezone.utc) - timedelta(days=10)
            ).isoformat(),
        )
        self.assertAlmostEqual(memory.effective_strength(), 0.5, delta=0.02)

    def test_zero_decay_stays_stable(self):
        memory = MemoryEntry(
            "m1",
            "u1",
            "核心身份",
            layer="core",
            strength=0.8,
            decay_rate=0,
            last_confirmed_at=(
                datetime.now(timezone.utc) - timedelta(days=1000)
            ).isoformat(),
        )
        self.assertAlmostEqual(memory.effective_strength(), 0.8, places=3)


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = await SQLiteDB(Path(self.temp.name) / "test.db").connect()
        await self.db.init_tables()
        self.memories = MemoryRepository(self.db)
        self.graph = GraphRepository(self.db)
        self.fifo = FifoRepository(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_duplicate_memory_is_reinforced(self):
        first = MemoryEntry("m1", "u1", "喜欢 Python", strength=0.5, confidence=0.6)
        second = MemoryEntry(
            "m2", "u1", "  喜欢   python  ", strength=0.5, confidence=0.6
        )
        await self.memories.upsert(first)
        saved = await self.memories.upsert(second)
        entries = await self.memories.get_by_user("u1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(saved.memory_id, "m1")
        self.assertEqual(entries[0].confirmation_count, 2)
        self.assertGreater(entries[0].strength, 0.5)

    async def test_delete_is_scoped_to_owner(self):
        await self.memories.upsert(MemoryEntry("m1", "u1", "私有事实"))
        self.assertFalse(await self.memories.delete("m1", "u2"))
        self.assertIsNotNone(await self.memories.get("m1", "u1"))
        self.assertTrue(await self.memories.delete("m1", "u1"))

    async def test_chinese_fts_atom_recall_is_owner_scoped(self):
        await self.memories.upsert(
            MemoryEntry("m-game", "u1", "用户喜欢杀戮尖塔", importance=4)
        )
        await self.memories.upsert(
            MemoryEntry("m-other", "u2", "用户也喜欢杀戮尖塔", importance=5)
        )
        result = await self.memories.search_atoms(
            "你还记得我喜欢杀戮尖塔吗？", "u1", limit=10
        )
        self.assertEqual(result.mode, "fts5")
        self.assertEqual([hit.memory.memory_id for hit in result.hits], ["m-game"])
        self.assertEqual(result.tokenizer, "trigram")

    async def test_like_and_background_atom_fallbacks(self):
        await self.memories.upsert(
            MemoryEntry("m-city", "u1", "用户住在上海", importance=2)
        )
        await self.memories.upsert(
            MemoryEntry("m-important", "u1", "用户是后端开发者", importance=5)
        )
        await self.db.conn.execute(
            "UPDATE meta SET value='0' WHERE key='fts_available'"
        )
        await self.db.conn.commit()
        like = await self.memories.search_atoms("上海", "u1", limit=10)
        self.assertEqual(like.mode, "like")
        self.assertEqual(like.hits[0].memory.memory_id, "m-city")
        background = await self.memories.search_atoms(
            "不存在的检索目标xyz", "u1", limit=2, background_limit=1
        )
        self.assertEqual(background.mode, "background")
        self.assertEqual(background.hits[0].memory.memory_id, "m-important")

    async def test_group_atom_is_scoped_to_its_context(self):
        await self.memories.upsert(
            MemoryEntry(
                "m-group-only",
                "u1",
                "开发群正在讨论秘密版本",
                visibility_scope="group",
                context_id="group:g1",
            )
        )
        visible = await self.memories.search_atoms(
            "秘密版本", "u1", context_id="group:g1"
        )
        hidden = await self.memories.search_atoms(
            "秘密版本", "u1", context_id="group:g2"
        )
        self.assertEqual(visible.hits[0].memory.memory_id, "m-group-only")
        self.assertEqual(hidden.hits, [])

    async def test_relation_reinforcement_and_group_visibility(self):
        async with self.db.transaction():
            await self.graph.upsert_entity_no_commit(Entity("user:u1", "user", "甲"))
            await self.graph.upsert_entity_no_commit(Entity("user:u2", "user", "乙"))
            relation = Relation(
                "r1",
                "user:u1",
                "friend_of",
                "user:u2",
                context_id="group:g1",
                visibility_scope="group",
                strength=0.5,
            )
            await self.graph.upsert_relation_no_commit(
                relation, RelationEvidence("e1", "r1", "乙是我的朋友", "u1")
            )
        self.assertEqual(
            await self.graph.get_neighbors("user:u1", 10, 0, "group:g2"), []
        )
        visible = await self.graph.get_neighbors("user:u1", 10, 0, "group:g1")
        self.assertEqual(len(visible), 1)
        async with self.db.transaction():
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "r2",
                    "user:u1",
                    "friend_of",
                    "user:u2",
                    context_id="group:g1",
                    visibility_scope="group",
                    strength=0.5,
                )
            )
        reinforced = await self.graph.get_neighbors("user:u1", 10, 0, "group:g1")
        self.assertEqual(reinforced[0].confirmation_count, 2)
        self.assertGreater(reinforced[0].strength, 0.5)

    async def test_fifo_isolated_by_context(self):
        await self.fifo.append_turn(
            "u1",
            ConversationTurn(
                "t1",
                "u1",
                "群一消息",
                "回复",
                "2026-01-01T00:00:00+00:00",
                "group:g1",
                "g1",
            ),
        )
        await self.fifo.append_turn(
            "u1",
            ConversationTurn(
                "t2",
                "u1",
                "群二消息",
                "回复",
                "2026-01-01T00:00:01+00:00",
                "group:g2",
                "g2",
            ),
        )
        group_one = await self.fifo.get_turns("u1", 10, "group:g1")
        self.assertEqual([turn.turn_id for turn in group_one], ["t1"])
        self.assertEqual(await self.fifo.count("u1", "group:g2"), 1)

    async def test_fifo_expired_streams(self):
        await self.fifo.append_turn(
            "u1",
            ConversationTurn(
                "old-turn",
                "u1",
                "旧消息",
                "回复",
                "2026-01-01T00:00:00+00:00",
                "group:g1",
                "g1",
            ),
        )
        await self.fifo.append_turn(
            "u2",
            ConversationTurn(
                "new-turn",
                "u2",
                "新消息",
                "回复",
                "2099-01-01T00:00:00+00:00",
                "group:g2",
                "g2",
            ),
        )
        expired = await self.fifo.get_expired_streams("2026-01-02T00:00:00+00:00")
        self.assertEqual(
            [(x["user_id"], x["context_id"]) for x in expired],
            [("u1", "group:g1")],
        )

    async def test_private_relation_only_visible_to_owner(self):
        async with self.db.transaction():
            await self.graph.upsert_entity_no_commit(Entity("user:u1", "user", "甲"))
            await self.graph.upsert_entity_no_commit(Entity("user:u2", "user", "乙"))
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "private-r",
                    "user:u1",
                    "trusts",
                    "user:u2",
                    visibility_scope="private",
                    owner_user_id="u1",
                )
            )
        self.assertEqual(len(await self.graph.get_neighbors("user:u1", 10, 0)), 1)
        self.assertEqual(await self.graph.get_neighbors("user:u2", 10, 0), [])

    async def test_sqlite_backup_is_created(self):
        backup = Path(self.temp.name) / "backup" / "snapshot.db"
        await self.db.vacuum_backup(backup)
        self.assertTrue(backup.exists())

    async def test_rule_based_graph_recall_without_embeddings(self):
        async with self.db.transaction():
            await self.graph.upsert_entity_no_commit(
                Entity("user:u1", "user", "当前用户")
            )
            await self.graph.upsert_entity_no_commit(
                Entity("user:u2", "user", "王明", ["小王"])
            )
            await self.graph.upsert_entity_no_commit(
                Entity("project:tiermem", "project", "TierMem", ["记忆插件"])
            )
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "r-colleague",
                    "user:u1",
                    "colleague_of",
                    "user:u2",
                    visibility_scope="public",
                    owner_user_id="u1",
                )
            )
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "r-project",
                    "user:u2",
                    "participates_in",
                    "project:tiermem",
                    visibility_scope="public",
                    owner_user_id="u2",
                )
            )
        retriever = GraphRetriever(PluginConfig(), self.graph)
        result = await retriever.recall(
            "u1", "小王负责的 TierMem 项目怎么样了？", "private:u1"
        )
        self.assertEqual(
            {m.entity.entity_id for m in result.matched_entities},
            {"user:u2", "project:tiermem"},
        )
        self.assertIn("participates_in", result.intents)
        self.assertEqual(result.scored_relations[0].relation.relation_id, "r-project")
        self.assertIn("直接连接两个锚点", result.scored_relations[0].reasons)

    async def test_atom_enters_graph_through_mentions_and_evidence(self):
        memory = MemoryEntry(
            "m-evidence",
            "u1",
            "小王负责 TierMem 的召回模块",
            layer="episodic",
            category="relation",
            importance=4,
        )
        await self.memories.upsert(memory)
        async with self.db.transaction():
            await self.graph.upsert_entity_no_commit(
                Entity("user:u1", "user", "当前用户")
            )
            await self.graph.upsert_entity_no_commit(Entity("user:u2", "user", "小王"))
            await self.graph.upsert_entity_no_commit(
                Entity("project:tiermem", "project", "TierMem")
            )
            await self.graph.link_memory_entities_no_commit(
                memory.memory_id, ["user:u2", "project:tiermem"], "evidence", 0.9
            )
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "r-evidence",
                    "user:u2",
                    "participates_in",
                    "project:tiermem",
                    visibility_scope="public",
                    owner_user_id="u1",
                ),
                RelationEvidence(
                    "e-atom",
                    "r-evidence",
                    memory.content,
                    "u1",
                    memory_id=memory.memory_id,
                    evidence_weight=0.9,
                ),
            )
        retriever = GraphRetriever(PluginConfig(), self.graph, self.memories)
        result = await retriever.recall(
            "u1", "TierMem 的召回模块是谁负责？", "private:u1"
        )
        self.assertEqual(result.atom_search.mode, "fts5")
        self.assertEqual(result.atom_search.hits[0].memory.memory_id, "m-evidence")
        self.assertEqual(
            {m.entity.entity_id for m in result.matched_entities},
            {"user:u2", "project:tiermem"},
        )
        self.assertEqual(result.evidence_edges[0].relation.relation_id, "r-evidence")
        self.assertEqual(result.scored_relations[0].relation.relation_id, "r-evidence")
        self.assertIn("召回原子直接支持此关系", result.scored_relations[0].reasons)

    async def test_atom_evidence_does_not_bypass_relation_privacy(self):
        memory = MemoryEntry("m-private-edge", "u1", "秘密项目由乙负责")
        await self.memories.upsert(memory)
        async with self.db.transaction():
            await self.graph.upsert_entity_no_commit(Entity("user:u2", "user", "乙"))
            await self.graph.upsert_entity_no_commit(
                Entity("project:secret", "project", "秘密项目")
            )
            await self.graph.link_memory_entities_no_commit(
                memory.memory_id, ["project:secret"]
            )
            await self.graph.upsert_relation_no_commit(
                Relation(
                    "r-private-owner",
                    "user:u2",
                    "participates_in",
                    "project:secret",
                    visibility_scope="private",
                    owner_user_id="u2",
                ),
                RelationEvidence(
                    "e-private-owner",
                    "r-private-owner",
                    memory.content,
                    "u2",
                    memory_id=memory.memory_id,
                ),
            )
        retriever = GraphRetriever(PluginConfig(), self.graph, self.memories)
        result = await retriever.recall("u1", "秘密项目是谁负责？", "private:u1")
        self.assertEqual(result.evidence_edges, [])
        self.assertEqual(result.scored_relations, [])


if __name__ == "__main__":
    unittest.main()
