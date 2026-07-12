# ruff: noqa: E402
import asyncio
import json
import sys
from collections import OrderedDict, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

_plugin_dir = Path(__file__).parent.resolve()
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from api.commands import CommandHandler
from api.tools import MemoryTools
from core.config import PluginConfig
from core.exceptions import SummaryError
from core.models import (
    ConversationTurn,
    Entity,
    GroupObservation,
    MemoryEntry,
    Relation,
    RelationEvidence,
    decay_rate_from_half_life,
    utc_now,
)
from service.backup import BackupService
from service.graph_retriever import GraphRetriever
from service.group_summarizer import GroupSummarizer
from service.injector import Injector
from service.passive_group_capture import (
    PassiveGroupCaptureFilter,
    session_allows_capture,
    set_active_plugin,
)
from service.summarizer import Summarizer
from storage.database import SQLiteDB
from storage.fifo_repo import FifoRepository
from storage.graph_repo import GraphRepository
from storage.group_observation_repo import GroupObservationRepository
from storage.memory_repo import MemoryRepository
from utils.id_gen import (
    generate_evidence_id,
    generate_memory_id,
    generate_relation_id,
    generate_turn_id,
)
from utils.subject import (
    detect_scene,
    extract_context_id,
    extract_group_id,
    extract_user_id,
)


@register("astrbot_TierMem", "TierMem", "原子记忆 + 关系知识图谱", "2.0.0")
class TierMemPlugin(Star):
    _NICKNAME_CACHE_MAX = 2000

    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.context = context
        self._raw_config = config if isinstance(config, dict) else {}
        self.config = PluginConfig.from_astrbot_config(self._raw_config)
        self.data_dir = Path(get_astrbot_data_path()) / "tiermem"
        self.db_path = self.data_dir / "tiermem.db"
        self.db = None
        self.mem_repo = None
        self.graph_repo = None
        self.fifo_repo = None
        self.group_observation_repo = None
        self.summarizer = None
        self.group_summarizer = None
        self.injector = None
        self.backup_service = None
        self.commands = None
        self.memory_tools = None
        self.graph_retriever = None
        self._initialized = False
        self._summary_semaphore = None
        self._user_locks = {}
        self._maintenance_lock = asyncio.Lock()
        self._pending = defaultdict(deque)
        self._nickname_cache = OrderedDict()
        self._scheduled_summaries = set()
        self._fifo_watchdog_task = None
        self._group_watchdog_task = None
        self._passive_capture_tasks = set()
        self._group_summary_tasks = {}
        self._group_locks = {}
        self._group_summary_failures = {}
        set_active_plugin(self)
        self._register_web_apis()

    async def initialize(self):
        if self._initialized:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = await SQLiteDB(self.db_path).connect()
        await self.db.init_tables()
        self._wire_services()
        self._summary_semaphore = asyncio.Semaphore(
            max(1, int(self.config.max_concurrent_summaries))
        )
        self._initialized = True
        self._warn_passive_group_policy()
        if self.config.enable_auto_summary and self.config.fifo_max_wait_minutes > 0:
            self._fifo_watchdog_task = asyncio.create_task(self._fifo_watchdog())
        if (
            self.config.enable_passive_group_capture
            and self.config.passive_group_max_wait_minutes > 0
        ):
            self._group_watchdog_task = asyncio.create_task(
                self._passive_group_watchdog()
            )
        logger.info("TierMem v2 初始化完成：原子记忆 + 知识图谱 + 惰性衰减")

    def _wire_services(self):
        self.mem_repo = MemoryRepository(self.db)
        self.graph_repo = GraphRepository(self.db)
        self.fifo_repo = FifoRepository(self.db)
        self.group_observation_repo = GroupObservationRepository(self.db)
        self.summarizer = Summarizer(self.config, self.context)
        self.group_summarizer = GroupSummarizer(self.config, self.context)
        self.injector = Injector(self.config)
        self.backup_service = BackupService(self.db, self.data_dir / "backup")
        self.commands = CommandHandler(
            self.config, self.mem_repo, self.fifo_repo, self.graph_repo
        )
        self.memory_tools = MemoryTools(self.config, self.mem_repo)
        self.graph_retriever = GraphRetriever(
            self.config, self.graph_repo, self.mem_repo
        )

    def _warn_passive_group_policy(self):
        ids = [str(item).strip() for item in self.config.passive_group_ids]
        if (
            self.config.enable_passive_group_capture
            and self.config.passive_group_filter_mode == "blacklist"
            and not any(ids)
        ):
            logger.warning(
                "TierMem 被动群聊观察已启用且黑名单为空：将捕获所有群的普通消息"
            )

    def _register_web_apis(self):
        if not hasattr(self.context, "register_web_api"):
            return
        routes = (
            ("stats", self.page_stats, ["GET"], "TierMem overview"),
            ("graph", self.page_graph, ["GET"], "TierMem graph"),
            ("memories", self.page_memories, ["GET"], "TierMem memories"),
            ("recall", self.page_recall, ["POST"], "TierMem recall lab"),
            ("settings", self.page_settings, ["GET", "POST"], "TierMem settings"),
        )
        for suffix, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/astrbot_TierMem/{suffix}", handler, methods, desc
            )

    @filter.custom_filter(PassiveGroupCaptureFilter, False)
    async def passive_group_capture_hook(self, event: AstrMessageEvent):
        """The filter schedules capture and returns False, so this body is not run."""
        return

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._initialized:
            await self.initialize()
        user_id, context_id, scene = (
            extract_user_id(event),
            extract_context_id(event),
            detect_scene(event),
        )
        user_text = event.message_str or ""
        nickname = self._remember_nickname(event, user_id)
        await self.graph_repo.ensure_user(user_id, nickname)

        should_inject = (
            scene == "private" and self.config.inject_memory_in_private
        ) or (scene == "group" and self.config.inject_memory_in_group)
        if should_inject:
            recall = await self.graph_retriever.recall(user_id, user_text, context_id)
            memories = recall.memories
            relations = recall.relations
            entities = await self._load_relation_entities(relations)
            fifo = None
            group_observations = None
            if scene == "group" and self.config.inject_fifo_in_group:
                fifo = await self.fifo_repo.get_turns(
                    user_id, self.config.fifo_size, context_id
                )
            group_id = extract_group_id(event)
            if (
                scene == "group"
                and group_id
                and self.config.allows_passive_group(group_id)
                and self.config.passive_group_recent_inject_limit > 0
            ):
                group_observations = await self.group_observation_repo.get_recent(
                    context_id, self.config.passive_group_recent_inject_limit
                )
            self.injector.update_nickname_cache(self._nickname_cache)
            req.system_prompt = (req.system_prompt or "") + self.injector.build_prompt(
                user_id,
                scene,
                memories,
                relations,
                entities,
                fifo,
                group_observations,
            )

        if self.config.enable_llm_tools and self.config.tool_caution_in_prompt:
            req.system_prompt = (req.system_prompt or "") + (
                "\n[NOTE] memory_add/update/delete 仅管理当前用户的原子记忆；"
                "用户间关系由知识图谱总结器维护。\n"
            )
        if user_text:
            pending_id = generate_turn_id()
            origin = event.unified_msg_origin
            self._pending[origin].append(
                {
                    "pending_id": pending_id,
                    "user_id": user_id,
                    "context_id": context_id,
                    "group_id": extract_group_id(event),
                    "user_message": user_text,
                    # 使用统一 ISO UTC 时间，保证数据库过期扫描可以可靠比较。
                    "timestamp": utc_now(),
                }
            )
            asyncio.create_task(self._expire_pending(origin, pending_id))

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self._initialized:
            return
        queue = self._pending.get(event.unified_msg_origin)
        if not queue:
            return
        pending = queue.popleft()
        if not queue:
            self._pending.pop(event.unified_msg_origin, None)
        turn = ConversationTurn(
            turn_id=generate_turn_id(),
            user_id=pending["user_id"],
            user_message=pending["user_message"],
            assistant_message=resp.completion_text or "",
            timestamp=pending["timestamp"],
            context_id=pending["context_id"],
            group_id=pending["group_id"],
        )
        try:
            await self.fifo_repo.append_turn(pending["user_id"], turn)
            if (
                self.config.enable_auto_summary
                and await self.fifo_repo.count(
                    pending["user_id"], pending["context_id"]
                )
                >= self.config.fifo_size
            ):
                self._schedule_summary(pending["user_id"], pending["context_id"])
        except Exception as exc:
            logger.error(f"保存对话失败: {exc}")

    def _schedule_passive_group_capture(self, event: AstrMessageEvent) -> None:
        if not self._initialized or not self.config.enable_passive_group_capture:
            return
        task = asyncio.create_task(self._capture_passive_group_message(event))
        self._passive_capture_tasks.add(task)
        task.add_done_callback(self._passive_capture_tasks.discard)

    async def _capture_passive_group_message(self, event: AstrMessageEvent):
        try:
            context_id = extract_context_id(event)
            group_id = extract_group_id(event)
            if not group_id or not self.config.allows_passive_group(group_id):
                return
            if not await session_allows_capture(event.unified_msg_origin):
                return
            sender_user_id = extract_user_id(event)
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                self_id = ""
            if self_id and sender_user_id == self_id:
                return
            content = str(getattr(event, "message_str", "") or "").strip()
            if len(
                content
            ) < self.config.passive_group_min_message_length or content.startswith("/"):
                return
            sender_name = self._event_sender_name(event, sender_user_id)
            message_obj = getattr(event, "message_obj", None)
            raw_message_id = getattr(message_obj, "message_id", None)
            observation_id = (
                f"{context_id}:{raw_message_id}"
                if raw_message_id
                else generate_turn_id()
            )
            inserted = await self.group_observation_repo.append(
                GroupObservation(
                    observation_id=observation_id,
                    context_id=context_id,
                    group_id=group_id,
                    sender_user_id=sender_user_id,
                    sender_name=sender_name,
                    content=content[:4000],
                )
            )
            if not inserted:
                return
            await self.group_observation_repo.trim(
                context_id, self.config.passive_group_max_buffer
            )
            if (
                await self.group_observation_repo.count(context_id)
                >= self.config.passive_group_fifo_size
            ):
                self._schedule_group_summary(context_id, group_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"被动群消息捕获失败: {exc}")

    @staticmethod
    def _event_sender_name(event, fallback: str):
        try:
            name = event.get_sender_name()
            if name:
                return str(name)
        except Exception:
            pass
        return str(
            next(
                (
                    getattr(event, key, None)
                    for key in ("sender_name", "group_nickname", "nickname")
                    if getattr(event, key, None)
                ),
                fallback,
            )
        )

    def _schedule_group_summary(self, context_id: str, group_id: str) -> bool:
        existing = self._group_summary_tasks.get(context_id)
        if existing and not existing.done():
            return False
        failure = self._group_summary_failures.get(context_id)
        if failure and datetime.now(timezone.utc) < failure["retry_after"]:
            return False

        async def runner():
            try:
                await self._run_group_summary(context_id, group_id)
            finally:
                self._group_summary_tasks.pop(context_id, None)

        self._group_summary_tasks[context_id] = asyncio.create_task(runner())
        return True

    async def _run_group_summary(self, context_id: str, group_id: str):
        if not self._initialized or not self.config.enable_passive_group_capture:
            return
        async with self._summary_semaphore:
            async with self._group_lock(context_id):
                observations = await self.group_observation_repo.get(
                    context_id, self.config.passive_group_max_buffer
                )
                if not observations:
                    return
                try:
                    await self.backup_service.create_backup()
                    self.backup_service.cleanup_old_backups(keep=5)
                    memories = await self.mem_repo.get_by_user(context_id)
                    relations = await self.graph_repo.get_neighbors(
                        context_id, 200, 0.0, context_id
                    )
                    result = await self.group_summarizer.summarize_group(
                        observations, memories, relations, context_id
                    )
                    await self._apply_group_summary(
                        context_id, group_id, observations, result
                    )
                    await self.mem_repo.prune(
                        context_id, self.config.max_memories_per_user
                    )
                    self._group_summary_failures.pop(context_id, None)
                    logger.info(f"群 {group_id} 观察总结完成: {result.summary[:80]}")
                except SummaryError as exc:
                    logger.error(f"群 {group_id} 观察总结校验失败: {exc}")
                    self._defer_group_summary_retry(context_id)
                except Exception as exc:
                    logger.exception(f"群 {group_id} 观察总结异常: {exc}")
                    self._defer_group_summary_retry(context_id)

    def _defer_group_summary_retry(self, context_id: str):
        previous = self._group_summary_failures.get(context_id, {"attempts": 0})
        attempts = previous["attempts"] + 1
        delay_minutes = min(60, 5 * (2 ** min(attempts - 1, 4)))
        self._group_summary_failures[context_id] = {
            "attempts": attempts,
            "retry_after": datetime.now(timezone.utc)
            + timedelta(minutes=delay_minutes),
        }

    async def _apply_group_summary(self, context_id, group_id, observations, result):
        participant_names = {
            item.sender_user_id: item.sender_name for item in observations
        }
        async with self.db.transaction():
            await self.graph_repo.upsert_entity_no_commit(
                Entity(context_id, "group", group_id)
            )
            for user_id, name in participant_names.items():
                await self.graph_repo.upsert_entity_no_commit(
                    Entity(f"user:{user_id}", "user", name)
                )

            prepared_relations = []
            for operation in result.relation_operations:
                source = self._entity(
                    operation.source_entity_id,
                    operation.source_entity_type,
                    operation.source_name,
                    operation.source_aliases,
                )
                target = self._entity(
                    operation.target_entity_id,
                    operation.target_entity_type,
                    operation.target_name,
                    operation.target_aliases,
                )
                await self.graph_repo.upsert_entity_no_commit(source)
                await self.graph_repo.upsert_entity_no_commit(target)
                prepared_relations.append((operation, source, target))

            for operation in result.memory_operations:
                memory = await self.mem_repo.upsert_no_commit(
                    MemoryEntry(
                        memory_id=generate_memory_id(),
                        owner_user_id=context_id,
                        content=operation.content,
                        layer=operation.layer,
                        category=operation.category,
                        importance=self._int_range(operation.importance, 1, 5, 3),
                        confidence=self._float_range(operation.confidence, 0.7),
                        strength=0.8,
                        stability=self._float_range(operation.stability, 0.5),
                        decay_rate=decay_rate_from_half_life(
                            self.config.half_life_for_layer(operation.layer)
                        ),
                        source="passive_group_summary",
                        source_turn_id=observations[-1].observation_id,
                        visibility_scope="group",
                        context_id=context_id,
                    )
                )
                await self.graph_repo.link_memory_entities_no_commit(
                    memory.memory_id,
                    operation.entity_ids,
                    mention_role="group_observation",
                    confidence=memory.confidence,
                )

            for operation, source, target in prepared_relations:
                relation = Relation(
                    relation_id=generate_relation_id(),
                    source_entity_id=source.entity_id,
                    relation_type=operation.relation_type,
                    target_entity_id=target.entity_id,
                    confidence=self._float_range(operation.confidence, 0.7),
                    strength=0.8,
                    stability=self._float_range(operation.stability, 0.5),
                    decay_rate=decay_rate_from_half_life(
                        self.config.relation_half_life_days
                    ),
                    visibility_scope="group",
                    context_id=context_id,
                    owner_user_id=context_id,
                )
                evidence = await self._relation_evidence_atom_no_commit(
                    operation.evidence,
                    relation,
                    context_id,
                    observations[-1].observation_id,
                    "support",
                )
                await self.graph_repo.upsert_relation_no_commit(relation, evidence)

            await self.group_observation_repo.clear_ids_no_commit(
                [item.observation_id for item in observations]
            )

    async def _passive_group_watchdog(self):
        max_wait_seconds = self.config.passive_group_max_wait_minutes * 60.0
        interval = min(60.0, max(10.0, max_wait_seconds / 4.0))
        try:
            while self._initialized and self.config.enable_passive_group_capture:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=max_wait_seconds)
                ).isoformat()
                streams = await self.group_observation_repo.get_expired_streams(cutoff)
                for stream in streams:
                    self._schedule_group_summary(
                        stream["context_id"], stream["group_id"]
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception(f"群观察超时扫描异常: {exc}")

    async def _run_summary(self, user_id: str, context_id: str):
        if not self._initialized:
            return
        async with self._summary_semaphore:
            async with self._user_lock(user_id):
                try:
                    turns = await self.fifo_repo.get_turns(
                        user_id, self.config.fifo_size, context_id
                    )
                    if not turns:
                        return
                    await self.backup_service.create_backup()
                    self.backup_service.cleanup_old_backups(keep=5)
                    memories = await self.mem_repo.get_by_user(user_id)
                    relations = await self.graph_repo.get_neighbors(
                        f"user:{user_id}", 100, 0.0, context_id
                    )
                    result = await self.summarizer.summarize(
                        turns, memories, relations, user_id, context_id
                    )
                    await self._apply_summary(
                        user_id, context_id, turns[-1], result, memories, relations
                    )
                    await self.fifo_repo.clear(user_id, context_id)
                    await self.mem_repo.prune(
                        user_id, self.config.max_memories_per_user
                    )
                    logger.info(f"用户 {user_id} 总结完成: {result.summary[:80]}")
                except SummaryError as exc:
                    logger.error(f"用户 {user_id} 总结校验失败: {exc}")
                    await self.fifo_repo.delete_oldest(
                        user_id, self.config.fifo_size, context_id
                    )
                except Exception as exc:
                    logger.exception(f"用户 {user_id} 总结异常: {exc}")

    def _schedule_summary(self, user_id: str, context_id: str) -> bool:
        key = (user_id, context_id)
        if key in self._scheduled_summaries:
            return False
        self._scheduled_summaries.add(key)

        async def runner():
            try:
                await self._run_summary(user_id, context_id)
            finally:
                self._scheduled_summaries.discard(key)

        asyncio.create_task(runner())
        return True

    async def _fifo_watchdog(self):
        max_wait_seconds = float(self.config.fifo_max_wait_minutes) * 60.0
        interval = min(60.0, max(10.0, max_wait_seconds / 4.0))
        try:
            while self._initialized:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=max_wait_seconds)
                ).isoformat()
                for stream in await self.fifo_repo.get_expired_streams(cutoff):
                    self._schedule_summary(stream["user_id"], stream["context_id"])
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception(f"FIFO 超时扫描异常: {exc}")

    async def _apply_summary(
        self, user_id, context_id, turn, result, memories, relations
    ):
        memory_index = {m.memory_id: m for m in memories}
        relation_index = {r.relation_id: r for r in relations}
        now = utc_now()
        async with self.db.transaction():
            for op in result.memory_operations:
                if op.action == "delete":
                    await self.mem_repo.deactivate_no_commit(op.memory_id, user_id)
                    continue
                if op.action in ("update", "reinforce"):
                    entry = memory_index[op.memory_id]
                    if op.action == "reinforce":
                        entry.strength = min(1.0, entry.effective_strength() + 0.15)
                        entry.confidence = min(1.0, entry.confidence + 0.03)
                        entry.stability = min(1.0, entry.stability + 0.03)
                        entry.confirmation_count += 1
                    else:
                        entry.content, entry.layer, entry.category = (
                            op.content,
                            op.layer,
                            op.category,
                        )
                        entry.importance = self._int_range(
                            op.importance, 1, 5, entry.importance
                        )
                        entry.confidence = self._float_range(
                            op.confidence, entry.confidence
                        )
                        entry.stability = self._float_range(
                            op.stability, entry.stability
                        )
                    entry.decay_rate = decay_rate_from_half_life(
                        self.config.half_life_for_layer(entry.layer)
                    )
                    entry.updated_at = entry.last_confirmed_at = now
                else:
                    scope = op.visibility_scope or "private"
                    entry = MemoryEntry(
                        memory_id=generate_memory_id(),
                        owner_user_id=user_id,
                        content=op.content,
                        layer=op.layer,
                        category=op.category,
                        importance=self._int_range(op.importance, 1, 5, 3),
                        confidence=self._float_range(op.confidence, 0.7),
                        strength=0.8,
                        stability=self._float_range(op.stability, 0.5),
                        decay_rate=decay_rate_from_half_life(
                            self.config.half_life_for_layer(op.layer)
                        ),
                        source_turn_id=turn.turn_id,
                        visibility_scope=scope,
                        context_id=context_id if scope == "group" else None,
                    )
                entry = await self.mem_repo.upsert_no_commit(entry)
                if op.entity_ids:
                    await self.graph_repo.link_memory_entities_no_commit(
                        entry.memory_id,
                        op.entity_ids,
                        mention_role="mention",
                        confidence=self._float_range(op.confidence, entry.confidence),
                    )

            for op in result.relation_operations:
                anchor = f"user:{user_id}"
                if op.action == "delete":
                    relation = relation_index[op.relation_id]
                    evidence = await self._relation_evidence_atom_no_commit(
                        op.evidence,
                        relation,
                        user_id,
                        turn.turn_id,
                        "refute",
                    )
                    if evidence:
                        await self.graph_repo.add_evidence_no_commit(evidence)
                    await self.graph_repo.deactivate_no_commit(op.relation_id, anchor)
                    continue
                if op.action in ("update", "reinforce"):
                    relation = relation_index[op.relation_id]
                    if op.action == "reinforce":
                        relation.strength = min(
                            1.0, relation.effective_strength() + 0.15
                        )
                        relation.confidence = min(1.0, relation.confidence + 0.03)
                        relation.stability = min(1.0, relation.stability + 0.03)
                        relation.confirmation_count += 1
                    else:
                        relation.confidence = self._float_range(
                            op.confidence, relation.confidence
                        )
                        relation.stability = self._float_range(
                            op.stability, relation.stability
                        )
                        relation.relation_type = (
                            op.relation_type or relation.relation_type
                        )
                    relation.updated_at = relation.last_confirmed_at = now
                else:
                    source = self._entity(
                        op.source_entity_id,
                        op.source_entity_type,
                        op.source_name,
                        op.source_aliases,
                    )
                    target = self._entity(
                        op.target_entity_id,
                        op.target_entity_type,
                        op.target_name,
                        op.target_aliases,
                    )
                    await self.graph_repo.upsert_entity_no_commit(source)
                    await self.graph_repo.upsert_entity_no_commit(target)
                    scope = op.visibility_scope or "private"
                    relation = Relation(
                        relation_id=generate_relation_id(),
                        source_entity_id=source.entity_id,
                        relation_type=op.relation_type,
                        target_entity_id=target.entity_id,
                        confidence=self._float_range(op.confidence, 0.7),
                        strength=0.8,
                        stability=self._float_range(op.stability, 0.5),
                        decay_rate=decay_rate_from_half_life(
                            self.config.relation_half_life_days
                        ),
                        visibility_scope=scope,
                        context_id=context_id if scope == "group" else None,
                        owner_user_id=user_id,
                    )
                evidence = await self._relation_evidence_atom_no_commit(
                    op.evidence,
                    relation,
                    user_id,
                    turn.turn_id,
                    "support",
                )
                await self.graph_repo.upsert_relation_no_commit(relation, evidence)

    def _entity(self, entity_id, entity_type, name, aliases=None):
        allowed = ("user", "group", "project", "organization", "topic", "other")
        kind = entity_type if entity_type in allowed else "other"
        if not entity_id or ":" not in entity_id:
            raise SummaryError(f"实体 ID 无效: {entity_id}")
        clean_aliases = [
            str(alias).strip()
            for alias in (aliases or [])
            if str(alias).strip() and str(alias).strip() != (name or entity_id)
        ]
        return Entity(
            entity_id=entity_id,
            entity_type=kind,
            name=name or entity_id,
            aliases=list(dict.fromkeys(clean_aliases))[:20],
        )

    async def _relation_evidence_atom_no_commit(
        self, excerpt, relation, user_id, turn_id, polarity
    ):
        if not excerpt:
            return None
        memory = await self.mem_repo.upsert_no_commit(
            MemoryEntry(
                memory_id=generate_memory_id(),
                owner_user_id=user_id,
                content=str(excerpt)[:500],
                layer="episodic",
                category="relation",
                importance=4,
                confidence=relation.confidence,
                strength=0.8,
                stability=relation.stability,
                decay_rate=decay_rate_from_half_life(
                    self.config.episodic_half_life_days
                ),
                source="relation_evidence",
                source_turn_id=turn_id,
                visibility_scope=relation.visibility_scope or "private",
                context_id=relation.context_id,
            )
        )
        await self.graph_repo.link_memory_entities_no_commit(
            memory.memory_id,
            [relation.source_entity_id, relation.target_entity_id],
            mention_role="evidence",
            confidence=relation.confidence,
        )
        return RelationEvidence(
            evidence_id=generate_evidence_id(),
            relation_id=relation.relation_id,
            excerpt=str(excerpt)[:500],
            speaker_user_id=user_id,
            turn_id=turn_id,
            memory_id=memory.memory_id,
            polarity=polarity,
            evidence_weight=relation.confidence,
        )

    async def _load_relation_entities(self, relations):
        result = {}
        for entity_id in {
            x for r in relations for x in (r.source_entity_id, r.target_entity_id)
        }:
            entity = await self.graph_repo.get_entity(entity_id)
            if entity:
                result[entity_id] = entity
        return result

    async def page_stats(self):
        if not self._initialized:
            await self.initialize()
        queries = {
            "memories": "SELECT COUNT(*) n FROM memories WHERE status='active'",
            "users": "SELECT COUNT(DISTINCT owner_user_id) n FROM memories WHERE status='active'",
            "entities": "SELECT COUNT(*) n FROM entities",
            "relations": "SELECT COUNT(*) n FROM relations WHERE status='active'",
            "fifo": "SELECT COUNT(*) n FROM fifo_buffer",
            "group_observations": "SELECT COUNT(*) n FROM group_observation_buffer",
        }
        stats = {}
        for key, sql in queries.items():
            async with self.db.conn.execute(sql) as cursor:
                stats[key] = (await cursor.fetchone())["n"]
        async with self.db.conn.execute(
            "SELECT layer,COUNT(*) n FROM memories WHERE status='active' GROUP BY layer"
        ) as cursor:
            stats["layers"] = {r["layer"]: r["n"] for r in await cursor.fetchall()}
        stats["fts"] = await self.db.fts_status()
        return json_response(stats)

    async def page_graph(self):
        if not self._initialized:
            await self.initialize()
        limit = max(1, min(500, request.query.get("limit", 200, type=int)))
        relations = await self.graph_repo.list_relations(limit)
        entity_ids = {
            x for r in relations for x in (r.source_entity_id, r.target_entity_id)
        }
        entities = {
            e.entity_id: e
            for e in await self.graph_repo.list_entities(max(limit * 2, 100))
        }
        nodes = []
        for entity_id in entity_ids:
            entity = entities.get(entity_id) or await self.graph_repo.get_entity(
                entity_id
            )
            if entity:
                nodes.append(
                    {
                        "id": entity.entity_id,
                        "type": entity.entity_type,
                        "name": entity.name,
                        "aliases": entity.aliases,
                    }
                )
        edges = [
            {
                "id": r.relation_id,
                "source": r.source_entity_id,
                "target": r.target_entity_id,
                "type": r.relation_type,
                "confidence": r.confidence,
                "strength": round(r.effective_strength(), 4),
                "scope": r.visibility_scope,
                "context_id": r.context_id,
                "owner_user_id": r.owner_user_id,
            }
            for r in relations
        ]
        return json_response({"nodes": nodes, "edges": edges})

    async def page_memories(self):
        if not self._initialized:
            await self.initialize()
        limit = max(1, min(500, request.query.get("limit", 200, type=int)))
        user_id = request.query.get("user_id", "") or ""
        layer = request.query.get("layer", "") or ""
        if layer and layer not in ("core", "semantic", "episodic", "working"):
            return error_response("invalid layer", status_code=400)
        memories = await self.mem_repo.list_recent(limit, user_id, layer)
        return json_response(
            {
                "items": [
                    {
                        "memory_id": m.memory_id,
                        "user_id": m.owner_user_id,
                        "content": m.content,
                        "layer": m.layer,
                        "category": m.category,
                        "importance": m.importance,
                        "confidence": m.confidence,
                        "strength": round(m.effective_strength(), 4),
                        "updated_at": m.updated_at,
                    }
                    for m in memories
                ]
            }
        )

    async def page_recall(self):
        if not self._initialized:
            await self.initialize()
        payload = await request.json(default={})
        user_id = str(payload.get("user_id", "")).strip()
        message = str(payload.get("message", "")).strip()
        context_id = str(payload.get("context_id", f"private:{user_id}")).strip()
        if not user_id or not message:
            return error_response("user_id and message are required", status_code=400)
        result = await self.graph_retriever.recall(user_id, message, context_id)
        atom_search = result.atom_search
        return json_response(
            {
                "query": message,
                "search": {
                    "mode": atom_search.mode if atom_search else "disabled",
                    "query_terms": atom_search.query_terms if atom_search else [],
                    "fts_available": atom_search.fts_available
                    if atom_search
                    else False,
                    "tokenizer": atom_search.tokenizer if atom_search else "like",
                },
                "atoms": [
                    {
                        "memory_id": hit.memory.memory_id,
                        "content": hit.memory.content,
                        "layer": hit.memory.layer,
                        "category": hit.memory.category,
                        "score": hit.score,
                        "components": {
                            "text": hit.text_score,
                            "strength": hit.strength_score,
                            "importance": hit.importance_score,
                            "confidence": hit.confidence_score,
                        },
                        "reasons": hit.reasons,
                    }
                    for hit in (atom_search.hits if atom_search else [])
                ],
                "atom_entities": [
                    {
                        "memory_id": memory_id,
                        "entities": [
                            {"id": entity.entity_id, "name": entity.name}
                            for entity in entities
                        ],
                    }
                    for memory_id, entities in result.atom_entities.items()
                ],
                "evidence_edges": [
                    {
                        "memory_id": edge.memory_id,
                        "relation_id": edge.relation.relation_id,
                        "source": edge.relation.source_entity_id,
                        "target": edge.relation.target_entity_id,
                        "type": edge.relation.relation_type,
                        "polarity": edge.evidence.polarity,
                        "weight": edge.evidence.evidence_weight,
                    }
                    for edge in result.evidence_edges
                ],
                "matched_entities": [
                    {
                        "id": m.entity.entity_id,
                        "name": m.entity.name,
                        "alias": m.alias,
                        "kind": m.match_kind,
                        "score": m.score,
                        "memory_ids": m.memory_ids,
                    }
                    for m in result.matched_entities
                ],
                "intents": result.intents,
                "relations": [
                    {
                        "id": x.relation.relation_id,
                        "source": x.relation.source_entity_id,
                        "target": x.relation.target_entity_id,
                        "type": x.relation.relation_type,
                        "score": x.score,
                        "reasons": x.reasons,
                        "strength": round(x.relation.effective_strength(), 4),
                        "confidence": x.relation.confidence,
                        "hop": x.hop,
                        "supporting_memory_ids": x.supporting_memory_ids,
                    }
                    for x in result.scored_relations
                ],
            }
        )

    async def page_settings(self):
        editable = {
            "fifo_size",
            "fifo_max_wait_minutes",
            "max_memories_per_user",
            "max_injected_memories",
            "max_injected_relations",
            "atom_fts_candidate_limit",
            "atom_like_candidate_limit",
            "atom_background_limit",
            "atom_query_term_limit",
            "graph_recall_max_hops",
            "graph_alias_min_length",
            "graph_max_matched_entities",
            "graph_entity_scan_limit",
            "retrieval_min_strength",
            "relation_intent_keywords",
            "core_half_life_days",
            "semantic_half_life_days",
            "episodic_half_life_days",
            "working_half_life_days",
            "relation_half_life_days",
            "enable_auto_summary",
            "enable_llm_tools",
            "inject_memory_in_private",
            "inject_memory_in_group",
            "inject_fifo_in_group",
            "enable_passive_group_capture",
            "passive_group_filter_mode",
            "passive_group_ids",
            "passive_group_fifo_size",
            "passive_group_max_wait_minutes",
            "passive_group_max_buffer",
            "passive_group_recent_inject_limit",
            "passive_group_min_message_length",
            "passive_group_summary_system_prompt",
            "max_concurrent_summaries",
            "summary_provider_id",
            "summary_system_prompt",
            "enable_manual_summary",
            "tool_caution_in_prompt",
        }
        if request.method == "GET":
            return json_response(
                {k: v for k, v in asdict(self.config).items() if k in editable}
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("settings payload must be an object", status_code=400)
        unknown = set(payload) - editable
        if unknown:
            return error_response(
                f"unsupported settings: {', '.join(sorted(unknown))}", status_code=400
            )
        try:
            candidate = {**asdict(self.config), **payload}
            updated = PluginConfig.from_astrbot_config(candidate)
            if updated.graph_recall_max_hops not in (1, 2):
                raise ValueError("graph_recall_max_hops must be 1 or 2")
            if updated.passive_group_filter_mode not in ("whitelist", "blacklist"):
                raise ValueError("passive_group_filter_mode is invalid")
            if not isinstance(updated.passive_group_ids, list):
                raise ValueError("passive_group_ids must be a list")
            if not isinstance(updated.relation_intent_keywords, dict):
                raise ValueError("relation_intent_keywords must be an object")
            if any(
                not isinstance(words, list)
                or not all(isinstance(word, str) and word.strip() for word in words)
                for words in updated.relation_intent_keywords.values()
            ):
                raise ValueError("each relation intent must contain a string list")
            numeric_ranges = {
                "fifo_size": (1, 100),
                "fifo_max_wait_minutes": (0, 10080),
                "max_memories_per_user": (20, 2000),
                "max_injected_memories": (1, 100),
                "max_injected_relations": (0, 100),
                "atom_fts_candidate_limit": (4, 500),
                "atom_like_candidate_limit": (4, 200),
                "atom_background_limit": (1, 20),
                "atom_query_term_limit": (4, 100),
                "graph_alias_min_length": (1, 16),
                "graph_max_matched_entities": (1, 20),
                "graph_entity_scan_limit": (100, 50000),
                "retrieval_min_strength": (0, 1),
                "max_concurrent_summaries": (1, 10),
                "core_half_life_days": (0, 36500),
                "semantic_half_life_days": (0.1, 36500),
                "episodic_half_life_days": (0.1, 36500),
                "working_half_life_days": (0.1, 36500),
                "relation_half_life_days": (0.1, 36500),
                "passive_group_fifo_size": (5, 500),
                "passive_group_max_wait_minutes": (0, 10080),
                "passive_group_max_buffer": (20, 5000),
                "passive_group_recent_inject_limit": (0, 100),
                "passive_group_min_message_length": (1, 100),
            }
            for key, (minimum, maximum) in numeric_ranges.items():
                value = getattr(updated, key)
                if not minimum <= value <= maximum:
                    raise ValueError(f"{key} must be between {minimum} and {maximum}")
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        for key in editable:
            setattr(self.config, key, getattr(updated, key))
            value = getattr(updated, key)
            self._raw_config[key] = (
                json.dumps(value, ensure_ascii=False)
                if key == "relation_intent_keywords"
                else value
            )
        save = getattr(self._raw_config, "save_config", None)
        if callable(save):
            save()
        self._summary_semaphore = asyncio.Semaphore(
            max(1, int(self.config.max_concurrent_summaries))
        )
        self._warn_passive_group_policy()
        await self._restart_watchdogs()
        return json_response({"saved": True})

    async def _restart_watchdogs(self):
        if self._fifo_watchdog_task:
            self._fifo_watchdog_task.cancel()
            try:
                await self._fifo_watchdog_task
            except asyncio.CancelledError:
                pass
            self._fifo_watchdog_task = None
        if (
            self._initialized
            and self.config.enable_auto_summary
            and self.config.fifo_max_wait_minutes > 0
        ):
            self._fifo_watchdog_task = asyncio.create_task(self._fifo_watchdog())
        if self._group_watchdog_task:
            self._group_watchdog_task.cancel()
            try:
                await self._group_watchdog_task
            except asyncio.CancelledError:
                pass
            self._group_watchdog_task = None
        if (
            self._initialized
            and self.config.enable_passive_group_capture
            and self.config.passive_group_max_wait_minutes > 0
        ):
            self._group_watchdog_task = asyncio.create_task(
                self._passive_group_watchdog()
            )

    def _user_lock(self, user_id):
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def _group_lock(self, context_id):
        if context_id not in self._group_locks:
            self._group_locks[context_id] = asyncio.Lock()
        return self._group_locks[context_id]

    async def _expire_pending(self, origin, pending_id):
        await asyncio.sleep(120)
        queue = self._pending.get(origin)
        if queue:
            self._pending[origin] = deque(
                x for x in queue if x["pending_id"] != pending_id
            )
            if not self._pending[origin]:
                self._pending.pop(origin, None)

    def _remember_nickname(self, event, user_id):
        name = next(
            (
                getattr(event, x, None)
                for x in ("sender_name", "nickname", "group_nickname")
                if getattr(event, x, None)
            ),
            user_id,
        )
        self._nickname_cache[user_id] = str(name)
        self._nickname_cache.move_to_end(user_id)
        while len(self._nickname_cache) > self._NICKNAME_CACHE_MAX:
            self._nickname_cache.popitem(last=False)
        return str(name)

    @staticmethod
    def _float_range(value, default):
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int_range(value, low, high, default):
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    @filter.command_group("memory")
    def memory_group(self, event: AstrMessageEvent, args: list):
        pass

    @memory_group.command("sum")
    async def cmd_sum(self, event: AstrMessageEvent):
        if not self.config.enable_manual_summary:
            yield event.plain_result("手动总结已禁用。")
            return
        self._schedule_summary(extract_user_id(event), extract_context_id(event))
        yield event.plain_result("总结任务已启动。")

    @memory_group.command("check")
    async def cmd_check(self, event: AstrMessageEvent):
        parts = (event.message_str or "").split()
        yield await self.commands.check(event, parts[2] if len(parts) > 2 else None)

    @memory_group.command("graph")
    async def cmd_graph(self, event: AstrMessageEvent):
        yield await self.commands.graph(event)

    @memory_group.command("fifo")
    async def cmd_fifo(self, event: AstrMessageEvent):
        yield await self.commands.fifo(event)

    @memory_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        yield await self.commands.status(event)

    @memory_group.command("clear")
    async def cmd_clear(self, event: AstrMessageEvent):
        yield await self.commands.clear(event)

    @memory_group.command("rollback")
    async def cmd_rollback(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("整库回滚仅限管理员。")
            return
        async with self._maintenance_lock:
            try:
                latest = self.backup_service.get_latest_backup()
                await self.db.close()
                import shutil

                shutil.copy2(latest, self.db_path)
                self.db = await SQLiteDB(self.db_path).connect()
                await self.db.init_tables()
                self._wire_services()
                yield event.plain_result(f"已回滚到 {latest.name}")
            except Exception as exc:
                logger.error(f"回滚失败: {exc}")
                yield event.plain_result(f"回滚失败: {exc}")

    @memory_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield self.commands.help(event)

    @filter.llm_tool(name="memory_add")
    async def tool_memory_add(
        self,
        event: AstrMessageEvent,
        content: str = "",
        layer: str = "semantic",
        category: str = "fact",
        importance: int = 3,
    ):
        return await self.memory_tools.memory_add(
            event, content, layer, category, importance
        )

    @filter.llm_tool(name="memory_update")
    async def tool_memory_update(
        self, event: AstrMessageEvent, memory_id: str = "", content: str = ""
    ):
        return await self.memory_tools.memory_update(event, memory_id, content)

    @filter.llm_tool(name="memory_delete")
    async def tool_memory_delete(self, event: AstrMessageEvent, memory_id: str = ""):
        return await self.memory_tools.memory_delete(event, memory_id)

    async def terminate(self):
        self._initialized = False
        set_active_plugin(None)
        if self._fifo_watchdog_task:
            self._fifo_watchdog_task.cancel()
            try:
                await self._fifo_watchdog_task
            except asyncio.CancelledError:
                pass
        if self._group_watchdog_task:
            self._group_watchdog_task.cancel()
            try:
                await self._group_watchdog_task
            except asyncio.CancelledError:
                pass
        tasks = [
            *self._passive_capture_tasks,
            *self._group_summary_tasks.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._passive_capture_tasks.clear()
        self._group_summary_tasks.clear()
        self._group_summary_failures.clear()
        if self.db:
            await self.db.close()
        logger.info("TierMem 已卸载")


# Preserve the historic class name for integrations importing it directly.
SmartMemoryPlugin = TierMemPlugin
