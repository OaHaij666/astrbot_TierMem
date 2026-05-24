from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api import logger
from core.config import PluginConfig
from storage.memory_repo import MemoryRepository
from storage.fifo_repo import FifoRepository
from service.backup import BackupService


class CommandHandler:
    def __init__(
        self,
        config: PluginConfig,
        mem_repo: MemoryRepository,
        fifo_repo: FifoRepository,
        backup_service: BackupService,
    ):
        self.config = config
        self.mem_repo = mem_repo
        self.fifo_repo = fifo_repo
        self.backup_service = backup_service

    async def handle(self, event: AstrMessageEvent, cmd: str, args: list) -> MessageEventResult:
        if cmd == "sum" or cmd == "summarize":
            return await self._cmd_summarize(event)
        elif cmd == "check":
            return await self._cmd_check(event, args)
        elif cmd == "clear":
            return await self._cmd_clear(event, args)
        elif cmd == "rollback":
            return await self._cmd_rollback(event)
        elif cmd == "status":
            return await self._cmd_status(event)
        elif cmd == "help":
            return await self._cmd_help(event)
        else:
            return event.plain_result("未知命令。使用 /memory help 查看帮助。")

    async def _cmd_summarize(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.config.enable_manual_summary:
            return event.plain_result("手动总结功能已禁用。")
        # 实际总结逻辑在 main.py 中通过 engine 触发
        return event.plain_result("请使用 /memory sum 命令触发总结（由主插件处理）。")

    async def _cmd_check(self, event: AstrMessageEvent, args: list) -> MessageEventResult:
        subject_id = self._extract_subject_id(event)
        layer = args[0] if args else None

        if layer and layer not in ("important", "general", "fleeting"):
            return event.plain_result(f"无效层级: {layer}。可选: important, general, fleeting")

        entries = await self.mem_repo.get_by_subject(subject_id, layer)
        if not entries:
            return event.plain_result("当前无记忆记录。")

        lines = [f"=== {layer or '全部'} 记忆 ==="]
        for e in entries:
            lines.append(f"[{e.layer}] {e.content[:80]} (id: {e.memory_id})")
        return event.plain_result("\n".join(lines))

    async def _cmd_clear(self, event: AstrMessageEvent, args: list) -> MessageEventResult:
        """清除自己的记忆和 FIFO"""
        subject_id = self._extract_subject_id(event)
        await self.mem_repo.delete_by_subject(subject_id)
        await self.fifo_repo.clear(subject_id)
        logger.info(f"用户清除了记忆: {subject_id}")
        return event.plain_result("已清除你的所有记忆和对话缓存。")

    async def _cmd_rollback(self, event: AstrMessageEvent) -> MessageEventResult:
        try:
            await self.backup_service.restore_latest()
            return event.plain_result("已回滚到上次备份。")
        except Exception as e:
            logger.error(f"回滚失败: {e}")
            return event.plain_result(f"回滚失败: {e}")

    async def _cmd_status(self, event: AstrMessageEvent) -> MessageEventResult:
        subject_id = self._extract_subject_id(event)
        fifo_count = await self.fifo_repo.count(subject_id)
        mem_counts = {
            "important": await self.mem_repo.count_by_subject_layer(subject_id, "important"),
            "general": await self.mem_repo.count_by_subject_layer(subject_id, "general"),
            "fleeting": await self.mem_repo.count_by_subject_layer(subject_id, "fleeting"),
        }
        lines = [
            "=== 记忆状态 ===",
            f"FIFO 缓存: {fifo_count} / {self.config.fifo_size}",
            f"Important: {mem_counts['important']}",
            f"General: {mem_counts['general']}",
            f"Fleeting: {mem_counts['fleeting']}",
            f"模式: {self.config.memory_mode}",
        ]
        return event.plain_result("\n".join(lines))

    async def _cmd_help(self, event: AstrMessageEvent) -> MessageEventResult:
        text = (
            "/memory sum - 立即触发总结\n"
            "/memory check [layer] - 查看记忆 (layer: important/general/fleeting)\n"
            "/memory clear - 清除你的所有记忆和对话缓存\n"
            "/memory rollback - 回滚到上次备份\n"
            "/memory status - 查看当前状态\n"
            "/memory admin_clear <user_id|all> - 管理员清除指定用户或所有记忆\n"
            "/memory help - 显示此帮助"
        )
        return event.plain_result(text)

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
