from astrbot.api import logger
from core.models import MemoryEntry
from core.config import PluginConfig
from storage.memory_repo import MemoryRepository
from utils.id_gen import generate_memory_id
from utils.subject import extract_subject_id


class MemoryTools:
    """LLM 工具方法集合（无装饰器，由主插件类统一注册）"""

    def __init__(self, config: PluginConfig, mem_repo: MemoryRepository):
        self.config = config
        self.mem_repo = mem_repo

    async def memory_add(
        self,
        event,
        content: str = "",
        layer: str = "general",
        category: str = "fact",
        importance: int = 3,
    ) -> str:
        """谨慎使用：添加一条新记忆。仅在用户明确提供了值得长期保存的新信息时使用。

        Args:
            content: 记忆内容
            layer: 记忆层级 (important/general/fleeting)
            category: 类别 (profile/preference/task/fact/event)
            importance: 重要程度 1-5
        """
        if not content:
            return "content 不能为空"
        if layer not in ("important", "general", "fleeting"):
            return f"无效的 layer: {layer}"

        subject_id = extract_subject_id(event, self.config.memory_mode)
        entry = MemoryEntry(
            memory_id=generate_memory_id(),
            content=content,
            layer=layer,
            category=category,
            importance=importance,
            subject_id=subject_id,
            source="tool_call",
        )
        await self.mem_repo.upsert(entry)
        logger.debug(f"[tool] memory_add: {entry.memory_id}")
        return f"已添加记忆 [{entry.memory_id}]: {content[:50]}..."

    async def memory_update(
        self,
        event,
        memory_id: str = "",
        content: str = "",
    ) -> str:
        """谨慎使用：更新一条已有记忆。仅用于纠正错误或过时的信息。

        Args:
            memory_id: 记忆唯一标识
            content: 更新后的内容
        """
        if not memory_id or not content:
            return "memory_id 和 content 不能为空"

        subject_id = extract_subject_id(event, self.config.memory_mode)
        entries = await self.mem_repo.get_by_subject(subject_id)
        target = None
        for e in entries:
            if e.memory_id == memory_id:
                target = e
                break
        if not target:
            return f"未找到记忆 {memory_id}"

        target.content = content
        target.source = "tool_call"
        await self.mem_repo.upsert(target)
        logger.debug(f"[tool] memory_update: {memory_id}")
        return f"已更新记忆 [{memory_id}]"

    async def memory_delete(
        self,
        event,
        memory_id: str = "",
    ) -> str:
        """谨慎使用：删除一条记忆。仅用于删除敏感、错误或重复的内容。

        Args:
            memory_id: 记忆唯一标识
        """
        if not memory_id:
            return "memory_id 不能为空"

        ok = await self.mem_repo.delete(memory_id)
        if ok:
            logger.debug(f"[tool] memory_delete: {memory_id}")
            return f"已删除记忆 [{memory_id}]"
        return f"未找到记忆 {memory_id}"

    async def memory_read_user(
        self,
        event,
        user_id: str = "",
        layer: str = "",
    ) -> str:
        """读取指定用户的记忆。在总结过程中如果发现需要更新其他用户的记忆时使用。

        Args:
            user_id: 目标用户的 ID
            layer: 记忆层级过滤 (important/general/fleeting)，留空则返回全部
        """
        if not user_id:
            return "user_id 不能为空"

        # 构建 subject_id（假设与当前用户同场景）
        from astrbot.api.event import AstrMessageEvent
        uid = event.unified_msg_origin
        parts = uid.split(":")
        msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"

        if self.config.memory_mode == "shared":
            subject_id = f"{user_id}#shared"
        elif msg_type == "GroupMessage":
            group_id = parts[-1] if parts else "unknown"
            subject_id = f"{user_id}#{group_id}"
        else:
            subject_id = f"{user_id}#private"

        entries = await self.mem_repo.get_by_subject(subject_id, layer or None)
        if not entries:
            return f"用户 {user_id} 在 {layer or '全部'} 层没有记忆记录。"

        lines = [f"=== 用户 {user_id} 的 {layer or '全部'} 记忆 ==="]
        for e in entries:
            lines.append(f"[{e.layer}] {e.content[:80]} (id: {e.memory_id})")
        return "\n".join(lines)

    def _extract_subject_id(self, event) -> str:
        return extract_subject_id(event, self.config.memory_mode)
