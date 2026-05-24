
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

# 确保插件内部包可被导入
_plugin_dir = Path(__file__).parent.resolve()
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))

from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from core.config import PluginConfig
from core.models import ConversationTurn, MemoryEntry
from core.exceptions import SummaryError

from storage.database import SQLiteDB
from storage.memory_repo import MemoryRepository
from storage.fifo_repo import FifoRepository
from storage.migration import ModeMigration

from service.summarizer import Summarizer
from service.injector import Injector
from service.backup import BackupService

from api.tools import MemoryTools
from api.commands import CommandHandler

from utils.id_gen import generate_turn_id


@register("astrbot_TierMem", "TierMem", "主动总结 + 工具辅助的双轨记忆系统", "1.0.0")
class SmartMemoryPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.context = context

        # 防御性处理：确保 config 是字典
        if not isinstance(config, dict):
            config = {}
        self.config = PluginConfig.from_astrbot_config(config)

        # 数据目录
        self.data_dir = Path(get_astrbot_data_path()) / "memory"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 数据库
        self.db_path = self.data_dir / "memory.db"
        self.db: SQLiteDB = None
        self.mem_repo: MemoryRepository = None
        self.fifo_repo: FifoRepository = None

        # 服务
        self.summarizer: Summarizer = None
        self.injector: Injector = None
        self.backup_service: BackupService = None
        self.cmd_handler: CommandHandler = None
        self.memory_tools: MemoryTools = None

        # 初始化标记
        self._initialized = False

        # 并发控制（延迟初始化，避免事件循环问题）
        self._summary_semaphore = None
        self._summary_round_counter: Dict[str, int] = {}

        # 记忆修改互斥锁：每个 subject_id 对应一个 asyncio.Lock
        self._memory_locks: Dict[str, asyncio.Lock] = {}

    async def initialize(self):
        """插件初始化"""
        if self._initialized:
            return

        # 连接数据库
        self.db = await SQLiteDB(self.db_path).connect()
        await self.db.init_tables()

        # Repository
        self.mem_repo = MemoryRepository(self.db.conn)
        self.fifo_repo = FifoRepository(self.db.conn)

        # 模式迁移检测
        migration = ModeMigration(self.db)
        await migration.check_and_run(self.config.memory_mode)

        # 服务初始化
        self.summarizer = Summarizer(self.config, self.context)
        self.injector = Injector(self.config)
        self.backup_service = BackupService(self.db, self.data_dir / "backup")
        self.cmd_handler = CommandHandler(
            self.config, self.mem_repo, self.fifo_repo, self.backup_service
        )
        self.memory_tools = MemoryTools(self.config, self.mem_repo)

        # 初始化信号量（使用 getattr 防止 schema 解析失败导致字段缺失）
        if self._summary_semaphore is None:
            max_concurrent = getattr(self.config, "max_concurrent_summaries", 2)
            self._summary_semaphore = asyncio.Semaphore(max_concurrent)

        self._initialized = True
        logger.info("SmartMemory 插件初始化完成")

    # ------------------------------------------------------------------
    # 事件监听
    # ------------------------------------------------------------------

    # 临时存储用户消息，用于在 on_llm_response 中配对
    _pending_user_messages: dict = {}

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求前注入记忆，并记录用户消息"""
        if not self._initialized:
            await self.initialize()

        subject_id = self._extract_subject_id(event)
        scene = self._detect_scene(event)

        # 读取记忆
        state = await self.mem_repo.get_state(subject_id)

        # 群聊时读取 FIFO
        fifo_turns = None
        if scene == "group" and self.config.inject_fifo_in_group:
            fifo_turns = await self.fifo_repo.get_turns(subject_id, self.config.fifo_size)

        # 构建注入文本
        mem_prompt = self.injector.build_memory_prompt(state, subject_id, scene, fifo_turns)

        # 追加到系统提示词
        req.system_prompt = (req.system_prompt or "") + mem_prompt

        # 工具使用警告
        if self.config.enable_llm_tools and self.config.tool_caution_in_prompt:
            req.system_prompt += (
                "\n[NOTE] 你拥有 memory_add / memory_update / memory_delete 工具，"
                "但请谨慎使用。记忆系统会自动总结对话，你只需在需要即时记录关键信息时调用工具。\n"
            )

        # 记录用户消息到 pending，等待 on_llm_response 配对
        user_text = event.message_str or ""
        if user_text:
            self._pending_user_messages[event.unified_msg_origin] = {
                "subject_id": subject_id,
                "user_message": user_text,
                "timestamp": getattr(event, "timestamp", None) or "",
            }

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 响应后，配对用户消息和助手回复，写入 FIFO"""
        if not self._initialized:
            return

        pending = self._pending_user_messages.pop(event.unified_msg_origin, None)
        if not pending:
            return

        assistant_text = resp.completion_text or ""

        turn = ConversationTurn(
            turn_id=generate_turn_id(),
            user_message=pending["user_message"],
            assistant_message=assistant_text,
            timestamp=pending["timestamp"],
            group_id=pending["subject_id"].split("#")[1] if "#" in pending["subject_id"] else None,
        )

        try:
            await self.fifo_repo.append_turn(pending["subject_id"], turn)

            # 检查是否触发总结
            if self.config.enable_auto_summary:
                count = await self.fifo_repo.count(pending["subject_id"])
                if count >= self.config.fifo_size:
                    # 后台异步总结，不阻塞
                    asyncio.create_task(self._run_summary(pending["subject_id"]))
        except Exception as e:
            logger.error(f"收集对话失败: {e}")

    def _get_memory_lock(self, subject_id: str) -> asyncio.Lock:
        """获取指定 subject_id 的记忆修改锁"""
        if subject_id not in self._memory_locks:
            self._memory_locks[subject_id] = asyncio.Lock()
        return self._memory_locks[subject_id]

    async def _run_summary(self, subject_id: str):
        """后台执行总结，带并发控制和互斥锁"""
        if self._summary_semaphore is None:
            max_concurrent = getattr(self.config, "max_concurrent_summaries", 2)
            self._summary_semaphore = asyncio.Semaphore(max_concurrent)
        async with self._summary_semaphore:
            # 获取该 subject 的互斥锁，防止多个总结任务同时修改同一用户记忆
            lock = self._get_memory_lock(subject_id)
            async with lock:
                try:
                    # 备份
                    await self.backup_service.create_backup()

                    # 读取 FIFO
                    turns = await self.fifo_repo.get_turns(subject_id, self.config.fifo_size)
                    if not turns:
                        return

                    # 读取当前记忆
                    state = await self.mem_repo.get_state(subject_id)

                    # 调用总结器
                    result = await self.summarizer.summarize(
                        turns, state, self.config.summary_mode
                    )

                    # 应用结果
                    await self._apply_summary_result(subject_id, result)

                    # 清空 FIFO
                    await self.fifo_repo.clear(subject_id)

                    # 更新总结轮次计数，淘汰过期 fleeting
                    await self._evict_fleeting_by_ttl(subject_id)

                    logger.info(f"总结完成: {subject_id}, summary: {result.summary[:50]}...")

                except SummaryError as e:
                    logger.error(f"总结失败（已保留 FIFO）: {e}")
                except Exception as e:
                    logger.error(f"总结异常: {e}")

    async def _evict_fleeting_by_ttl(self, subject_id: str):
        """按 TTL 轮次淘汰 fleeting 记忆"""
        self._summary_round_counter[subject_id] = self._summary_round_counter.get(subject_id, 0) + 1
        rounds = self._summary_round_counter[subject_id]

        ttl = getattr(self.config, "fleeting_ttl_rounds", 3)
        if rounds >= ttl:
            # 清除该 subject 的所有 fleeting
            entries = await self.mem_repo.get_by_subject(subject_id, "fleeting")
            for e in entries:
                await self.mem_repo.delete(e.memory_id)
            logger.info(f"fleeting 记忆已淘汰: {subject_id}（{len(entries)} 条，存活 {rounds} 轮）")
            self._summary_round_counter[subject_id] = 0

    async def _apply_summary_result(self, subject_id: str, result):
        """应用总结结果到数据库"""
        if result.mode == "full_replace" and result.full_state:
            await self.mem_repo.replace_state(subject_id, result.full_state)
        elif result.mode == "search_replace":
            state = await self.mem_repo.get_state(subject_id)
            index = {e.memory_id: e for e in state.all_entries()}

            for op in result.operations:
                if op.action == "add":
                    entry = MemoryEntry(
                        memory_id=generate_turn_id().replace("turn", "mem"),
                        content=op.content or "",
                        layer=op.layer or "general",
                        category=op.category or "fact",
                        importance=op.importance or 3,
                        subject_id=subject_id,
                        source="auto_summary",
                    )
                    await self.mem_repo.upsert(entry)
                elif op.action == "update" and op.memory_id:
                    if op.memory_id in index:
                        entry = index[op.memory_id]
                        entry.content = op.content or entry.content
                        from datetime import datetime, timezone
                        entry.updated_at = datetime.now(timezone.utc).isoformat()
                        await self.mem_repo.upsert(entry)
                elif op.action == "delete" and op.memory_id:
                    await self.mem_repo.delete(op.memory_id)
                # keep: 什么都不做

        # 淘汰溢出
        await self._evict_if_overflow(subject_id)

    async def _evict_if_overflow(self, subject_id: str):
        """按层淘汰超出限制的记忆（fleeting 不在这里淘汰，由 TTL 控制）"""
        for layer in ("important", "general"):
            count = await self.mem_repo.count_by_subject_layer(subject_id, layer)
            if count > self.config.max_memory_per_layer:
                entries = await self.mem_repo.get_by_subject(subject_id, layer)
                # 按 importance 升序，再按 updated_at 升序，淘汰老的
                entries.sort(key=lambda e: (e.importance, e.updated_at))
                to_delete = entries[: count - self.config.max_memory_per_layer]
                for e in to_delete:
                    await self.mem_repo.delete(e.memory_id)
                    logger.info(f"淘汰记忆: {e.memory_id}")

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @filter.command_group("memory")
    def memory_group(self, event: AstrMessageEvent, args: list):
        pass

    @memory_group.command("sum")
    async def cmd_sum(self, event: AstrMessageEvent):
        """手动触发总结"""
        if not self.config.enable_manual_summary:
            yield event.plain_result("手动总结功能已禁用。")
            return

        subject_id = self._extract_subject_id(event)
        asyncio.create_task(self._run_summary(subject_id))
        yield event.plain_result("总结任务已在后台启动。")

    @memory_group.command("summarize")
    async def cmd_summarize(self, event: AstrMessageEvent):
        """手动触发总结（别名）"""
        async for result in self.cmd_sum(event):
            yield result

    @memory_group.command("check")
    async def cmd_check(self, event: AstrMessageEvent):
        """查看记忆"""
        # 从消息文本解析参数，例如 /memory check important
        text = event.message_str or ""
        parts = text.strip().split()
        args = parts[2:] if len(parts) > 2 else []
        result = await self.cmd_handler.handle(event, "check", args)
        yield result

    @memory_group.command("rollback")
    async def cmd_rollback(self, event: AstrMessageEvent):
        """回滚记忆"""
        result = await self.cmd_handler.handle(event, "rollback", [])
        yield result

    @memory_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看状态"""
        result = await self.cmd_handler.handle(event, "status", [])
        yield result

    @memory_group.command("clear")
    async def cmd_clear(self, event: AstrMessageEvent):
        """清除自己的记忆"""
        result = await self.cmd_handler.handle(event, "clear", [])
        yield result

    @memory_group.command("admin_clear")
    async def cmd_admin_clear(self, event: AstrMessageEvent):
        """管理员清除指定用户或所有用户的记忆"""
        if not event.is_admin():
            yield event.plain_result("无权使用此命令，需要管理员权限。")
            return

        text = event.message_str or ""
        parts = text.strip().split()
        target = parts[2] if len(parts) > 2 else None

        if not target:
            yield event.plain_result("用法: /memory admin_clear <user_id|all>")
            return

        if target == "all":
            # 清除所有记忆和 FIFO
            subjects = await self.mem_repo.list_all_subjects()
            for sid in subjects:
                await self.mem_repo.delete_by_subject(sid)
                await self.fifo_repo.clear(sid)
            logger.info(f"管理员 {event.get_sender_id()} 清除了所有用户记忆")
            yield event.plain_result(f"已清除所有用户的记忆和 FIFO（共 {len(subjects)} 个 subject）。")
        else:
            # 清除指定用户
            # target 可以是 user_id，需要匹配所有相关 subject_id
            async with self.db.conn.execute(
                "SELECT DISTINCT subject_id FROM memories WHERE subject_id LIKE ?",
                (f"{target}#%",),
            ) as cursor:
                rows = await cursor.fetchall()
            subjects = [row["subject_id"] for row in rows]

            # 也检查 fifo_buffer
            async with self.db.conn.execute(
                "SELECT DISTINCT subject_id FROM fifo_buffer WHERE subject_id LIKE ?",
                (f"{target}#%",),
            ) as cursor:
                rows = await cursor.fetchall()
            subjects += [row["subject_id"] for row in rows]
            subjects = list(set(subjects))

            if not subjects:
                yield event.plain_result(f"未找到用户 {target} 的记忆记录。")
                return

            for sid in subjects:
                await self.mem_repo.delete_by_subject(sid)
                await self.fifo_repo.clear(sid)
            logger.info(f"管理员 {event.get_sender_id()} 清除了用户 {target} 的记忆")
            yield event.plain_result(f"已清除用户 {target} 的记忆和 FIFO（共 {len(subjects)} 个上下文）。")

    @memory_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助"""
        result = await self.cmd_handler.handle(event, "help", [])
        yield result

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @filter.llm_tool(name="memory_add")
    async def tool_memory_add(
        self,
        event: AstrMessageEvent,
        content: str = "",
        layer: str = "general",
        category: str = "fact",
        importance: int = 3,
    ) -> str:
        return await self.memory_tools.memory_add(
            event, content=content, layer=layer, category=category, importance=importance
        )

    @filter.llm_tool(name="memory_update")
    async def tool_memory_update(
        self,
        event: AstrMessageEvent,
        memory_id: str = "",
        content: str = "",
    ) -> str:
        return await self.memory_tools.memory_update(
            event, memory_id=memory_id, content=content
        )

    @filter.llm_tool(name="memory_delete")
    async def tool_memory_delete(
        self,
        event: AstrMessageEvent,
        memory_id: str = "",
    ) -> str:
        return await self.memory_tools.memory_delete(event, memory_id=memory_id)

    @filter.llm_tool(name="memory_read_user")
    async def tool_memory_read_user(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        layer: str = "",
    ) -> str:
        """读取指定用户的记忆。在总结过程中如果发现需要更新其他用户的记忆时使用。"""
        return await self.memory_tools.memory_read_user(event, user_id=user_id, layer=layer)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _extract_subject_id(self, event: AstrMessageEvent) -> str:
        uid = event.unified_msg_origin
        parts = uid.split(":")
        user_id = parts[-1] if parts else "unknown"
        msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"

        if self.config.memory_mode == "shared":
            return f"{user_id}#shared"

        if msg_type == "GroupMessage":
            group_id = parts[-1] if parts else "unknown"
            sender_id = event.get_sender_id() or user_id
            return f"{sender_id}#{group_id}"
        else:
            return f"{user_id}#private"

    def _detect_scene(self, event: AstrMessageEvent) -> str:
        uid = event.unified_msg_origin
        parts = uid.split(":")
        msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"
        return "group" if msg_type == "GroupMessage" else "private"



    async def terminate(self):
        """插件卸载"""
        if self.db:
            await self.db.close()
            logger.info("SmartMemory 插件已卸载，数据库连接已关闭")
