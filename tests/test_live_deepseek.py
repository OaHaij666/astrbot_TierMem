"""Optional end-to-end tests against DeepSeek's OpenAI-compatible API.

The real key is read from the repository-local .env file.  This module is skipped
unless TIERMEM_RUN_LIVE_TESTS=1 and AstrBot is importable in the active Python
environment, so normal unit-test runs never spend API quota.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class LiveSettings:
    api_key: str
    base_url: str
    model: str
    enabled: bool

    @classmethod
    def load(cls) -> "LiveSettings":
        file_values = load_dotenv(ROOT / ".env")
        value = lambda key, default="": os.environ.get(  # noqa: E731
            key, file_values.get(key, default)
        )
        return cls(
            api_key=value("DEEPSEEK_API_KEY"),
            base_url=value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=value("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            enabled=value("TIERMEM_RUN_LIVE_TESTS", "0").lower()
            in {"1", "true", "yes", "on"},
        )


SETTINGS = LiveSettings.load()
ASTRBOT_AVAILABLE = importlib.util.find_spec("astrbot") is not None and (
    importlib.util.find_spec("astrbot.api") is not None
)
LIVE_REASON = (
    "set TIERMEM_RUN_LIVE_TESTS=1 and provide DEEPSEEK_API_KEY in .env; "
    "run with AstrBot's Python environment"
)


class DeepSeekProvider:
    def __init__(self, settings: LiveSettings):
        self.settings = settings
        self.last_usage: dict = {}

    def _request(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload else None
        request = urllib.request.Request(
            f"{self.settings.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise AssertionError(
                f"DeepSeek API returned HTTP {exc.code}: {detail}"
            ) from exc

    async def list_models(self) -> list[str]:
        payload = await asyncio.to_thread(self._request, "GET", "/models")
        return [item["id"] for item in payload.get("data", [])]

    async def text_chat(self, prompt: str, system_prompt: str = "", **_kwargs):
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 3000,
            "stream": False,
        }
        response = await asyncio.to_thread(
            self._request, "POST", "/chat/completions", payload
        )
        self.last_usage = response.get("usage", {})
        content = response["choices"][0]["message"].get("content") or ""
        return SimpleNamespace(completion_text=content)


class MockContext:
    def __init__(self, provider):
        self.provider = provider
        self.routes = []

    def get_using_provider(self):
        return self.provider

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, tuple(methods), desc, handler))


@unittest.skipUnless(ASTRBOT_AVAILABLE, "run with AstrBot's Python environment")
class AstrBotPassiveFilterTests(unittest.TestCase):
    def test_packaged_import_survives_stale_core_models_cache(self):
        script = f"""
import importlib
import sys
import types
sys.path.insert(0, {str(ROOT)!r})
stale = importlib.import_module('core.models')
del stale.GroupObservation
data = types.ModuleType('data')
data.__path__ = []
plugins = types.ModuleType('data.plugins')
plugins.__path__ = []
package = types.ModuleType('data.plugins.astrbot_TierMem')
package.__path__ = [{str(ROOT)!r}]
sys.modules['data'] = data
sys.modules['data.plugins'] = plugins
sys.modules['data.plugins.astrbot_TierMem'] = package
plugin = importlib.import_module('data.plugins.astrbot_TierMem.main')
assert plugin.TierMemPlugin.__name__ == 'TierMemPlugin'
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT.parent,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )

    def test_group_tap_emits_snapshot_without_waking_bot(self):
        sys.path.insert(0, str(ROOT))
        from astrbot.api.platform import MessageType
        from service.passive_group_capture import (
            PassiveGroupMessageTap,
            bind_capture_sink,
            unbind_capture_sink,
        )

        snapshots = []
        token = bind_capture_sink(snapshots.append)
        event = Mock()
        event.get_message_type.return_value = MessageType.GROUP_MESSAGE
        event.get_group_id.return_value = "g1"
        event.get_sender_id.return_value = "u1"
        event.get_sender_name.return_value = "小林"
        event.get_self_id.return_value = "bot"
        event.unified_msg_origin = "platform:GroupMessage:g1"
        event.message_str = "普通群消息"
        event.message_obj = SimpleNamespace(message_id="m1")
        try:
            self.assertFalse(PassiveGroupMessageTap(False).filter(event, {}))
        finally:
            unbind_capture_sink(token)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].context_id, "group:g1")
        self.assertEqual(snapshots[0].sender_user_id, "u1")
        self.assertEqual(snapshots[0].content, "普通群消息")

    def test_non_group_event_never_reaches_sink(self):
        sys.path.insert(0, str(ROOT))
        from astrbot.api.platform import MessageType
        from service.passive_group_capture import (
            PassiveGroupMessageTap,
            bind_capture_sink,
            unbind_capture_sink,
        )

        snapshots = []
        token = bind_capture_sink(snapshots.append)
        event = Mock()
        event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
        try:
            self.assertFalse(PassiveGroupMessageTap(False).filter(event, {}))
        finally:
            unbind_capture_sink(token)
        self.assertEqual(snapshots, [])


@unittest.skipUnless(
    SETTINGS.enabled and bool(SETTINGS.api_key) and ASTRBOT_AVAILABLE,
    LIVE_REASON,
)
class DeepSeekLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_is_available(self):
        provider = DeepSeekProvider(SETTINGS)
        self.assertIn(SETTINGS.model, await provider.list_models())

    async def test_summary_storage_and_atom_first_recall_pipeline(self):
        sys.path.insert(0, str(ROOT))
        import main as tiermem_main
        from astrbot.api.platform import MessageType
        from astrbot.api.web import PluginRequest, bind_request_context
        from core.models import ConversationTurn, GroupObservation, utc_now
        from service.passive_group_capture import PassiveGroupMessageTap
        from starlette.requests import Request

        def plugin_request(method: str, path: str, payload: dict | None = None):
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "root_path": "",
            }
            return PluginRequest(Request(scope, receive))

        provider = DeepSeekProvider(SETTINGS)
        context = MockContext(provider)
        original_data_path = tiermem_main.get_astrbot_data_path
        temp_dir = tempfile.TemporaryDirectory()
        plugin = None
        try:
            tiermem_main.get_astrbot_data_path = lambda: temp_dir.name
            plugin = tiermem_main.TierMemPlugin(
                context,
                {
                    "enable_auto_summary": False,
                    "summary_system_prompt": (
                        "测试数据中 user:u2 是已确认的稳定用户 ID。"
                        "必须提取当前用户喜欢杀戮尖塔的 preference 原子，"
                        "以及 user:u1 与 user:u2 的 friend_of 关系。"
                    ),
                    "passive_group_summary_system_prompt": (
                        "测试消息中的发布计划已经由多人明确确认。"
                        "必须提取 TierMem v2 周五 20:00 发布这一 episodic/event 原子。"
                    ),
                    "enable_passive_group_capture": True,
                    "passive_group_ids": ["g-capture", "g-test"],
                    "passive_group_max_wait_minutes": 0,
                },
            )
            await plugin.initialize()
            capture_event = Mock()
            capture_event.get_message_type.return_value = MessageType.GROUP_MESSAGE
            capture_event.get_group_id.return_value = "g-capture"
            capture_event.get_sender_id.return_value = "u-capture"
            capture_event.get_sender_name.return_value = "旁听用户"
            capture_event.get_self_id.return_value = "bot"
            capture_event.unified_msg_origin = "platform:GroupMessage:g-capture"
            capture_event.message_str = "这是没有唤醒 Bot 的普通群消息"
            capture_event.message_obj = SimpleNamespace(message_id="capture-live-1")
            self.assertFalse(PassiveGroupMessageTap(False).filter(capture_event, {}))
            await asyncio.gather(*list(plugin.group_observer._ingest_tasks))
            self.assertEqual(
                await plugin.group_observation_repo.count("group:g-capture"), 1
            )
            await plugin.group_observation_repo.clear_context("group:g-capture")

            turn = ConversationTurn(
                turn_id="live-turn-1",
                user_id="u1",
                user_message=(
                    "我喜欢玩杀戮尖塔。用户 ID 是 u2 的小王是我的朋友，"
                    "他正在和我一起开发 TierMem。"
                ),
                assistant_message="记住了，你喜欢杀戮尖塔，小王是你的朋友。",
                timestamp=utc_now(),
                context_id="private:u1",
            )
            result = await plugin.summarizer.summarize(
                [turn], [], [], "u1", "private:u1"
            )

            self.assertTrue(result.memory_operations)
            self.assertTrue(result.relation_operations)
            self.assertTrue(
                any(
                    op.category == "preference"
                    and op.content
                    and "杀戮尖塔" in op.content
                    for op in result.memory_operations
                )
            )
            self.assertTrue(
                any(
                    op.relation_type == "friend_of"
                    and {op.source_entity_id, op.target_entity_id}
                    == {"user:u1", "user:u2"}
                    and op.evidence
                    for op in result.relation_operations
                )
            )

            await plugin._apply_summary("u1", "private:u1", turn, result, [], [])
            preference_recall = await plugin.graph_retriever.recall(
                "u1", "你还记得我喜欢杀戮尖塔吗？", "private:u1"
            )
            self.assertEqual(preference_recall.atom_search.mode, "fts5")
            self.assertTrue(
                any(
                    "杀戮尖塔" in memory.content
                    for memory in preference_recall.memories
                )
            )

            async with plugin.db.conn.execute(
                """SELECT m.content FROM relation_evidence re
                JOIN memories m ON m.memory_id=re.memory_id
                WHERE re.polarity='support' LIMIT 1"""
            ) as cursor:
                evidence_row = await cursor.fetchone()
            self.assertIsNotNone(evidence_row)
            evidence_recall = await plugin.graph_retriever.recall(
                "u1", evidence_row["content"], "private:u1"
            )
            self.assertTrue(evidence_recall.evidence_edges)
            self.assertTrue(
                any(
                    item.relation.relation_type == "friend_of"
                    for item in evidence_recall.scored_relations
                )
            )

            async with plugin.db.conn.execute(
                """SELECT
                (SELECT COUNT(*) FROM memories WHERE status='active') memories,
                (SELECT COUNT(*) FROM relations WHERE status='active') relations,
                (SELECT COUNT(*) FROM relation_evidence WHERE memory_id IS NOT NULL) evidence,
                (SELECT COUNT(*) FROM memory_entity_mentions) mentions"""
            ) as cursor:
                counts = await cursor.fetchone()
            self.assertGreaterEqual(counts["memories"], 2)
            self.assertGreaterEqual(counts["relations"], 1)
            self.assertGreaterEqual(counts["evidence"], 1)
            self.assertGreaterEqual(counts["mentions"], 2)
            self.assertEqual(len(context.routes), 5)
            self.assertGreater(provider.last_usage.get("total_tokens", 0), 0)

            stats_response = await plugin.page_stats()
            stats = json.loads(stats_response.body)
            self.assertGreaterEqual(stats["memories"], 2)
            self.assertTrue(stats["fts"]["available"])

            with bind_request_context(
                plugin_request("GET", "/astrbot_TierMem/settings")
            ):
                settings_response = await plugin.page_settings()
            settings = json.loads(settings_response.body)
            self.assertEqual(settings["atom_background_limit"], 4)
            self.assertIsInstance(settings["relation_intent_keywords"], dict)

            with bind_request_context(
                plugin_request(
                    "POST",
                    "/astrbot_TierMem/recall",
                    {
                        "user_id": "u1",
                        "context_id": "private:u1",
                        "message": evidence_row["content"],
                    },
                )
            ):
                recall_response = await plugin.page_recall()
            recall_payload = json.loads(recall_response.body)
            self.assertEqual(recall_payload["search"]["mode"], "fts5")
            self.assertTrue(recall_payload["atoms"])
            self.assertTrue(recall_payload["evidence_edges"])
            self.assertTrue(recall_payload["relations"])

            group_observations = [
                GroupObservation(
                    "group-live-1",
                    "group:g-test",
                    "g-test",
                    "u1",
                    "小林",
                    "TierMem v2 的发布时间确定为本周五 20:00。",
                ),
                GroupObservation(
                    "group-live-2",
                    "group:g-test",
                    "g-test",
                    "u2",
                    "小王",
                    "确认周五 20:00 发布，我负责发布文档。",
                ),
                GroupObservation(
                    "group-live-3",
                    "group:g-test",
                    "g-test",
                    "u1",
                    "小林",
                    "好的，发布计划就按这个时间执行。",
                ),
            ]
            for observation in group_observations:
                await plugin.group_observation_repo.append(observation)
            group_result = await plugin.group_summarizer.summarize_group(
                group_observations, [], [], "group:g-test"
            )
            self.assertTrue(
                any(
                    operation.category == "event"
                    and operation.content
                    and "周五" in operation.content
                    and "20:00" in operation.content
                    for operation in group_result.memory_operations
                )
            )
            await plugin._apply_group_summary(
                "group:g-test", "g-test", group_observations, group_result
            )
            self.assertEqual(
                await plugin.group_observation_repo.count("group:g-test"), 0
            )
            group_recall = await plugin.graph_retriever.recall(
                "u3", "TierMem v2 是什么时候发布？", "group:g-test"
            )
            self.assertTrue(
                any(
                    memory.owner_user_id == "group:g-test" and "周五" in memory.content
                    for memory in group_recall.memories
                )
            )
            private_group_leak = await plugin.graph_retriever.recall(
                "u3", "TierMem v2 是什么时候发布？", "private:u3"
            )
            self.assertFalse(
                any(
                    memory.owner_user_id == "group:g-test"
                    for memory in private_group_leak.memories
                )
            )
        finally:
            if plugin is not None:
                await plugin.terminate()
            tiermem_main.get_astrbot_data_path = original_data_path
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
