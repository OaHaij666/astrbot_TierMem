from core.config import PluginConfig
from core.models import MemoryEntry, decay_rate_from_half_life, utc_now
from storage.memory_repo import MemoryRepository
from utils.id_gen import generate_memory_id
from utils.subject import extract_user_id


LAYERS = ("core", "semantic", "episodic", "working")
CATEGORIES = ("profile", "preference", "task", "fact", "event")


class MemoryTools:
    def __init__(self, config: PluginConfig, mem_repo: MemoryRepository):
        self.config, self.mem_repo = config, mem_repo

    async def memory_add(
        self, event, content="", layer="semantic", category="fact", importance=3
    ):
        if not self.config.enable_llm_tools:
            return "记忆工具已禁用"
        if not content.strip() or layer not in LAYERS or category not in CATEGORIES:
            return "记忆内容、层级或分类无效"
        importance = max(1, min(5, int(importance)))
        user_id = extract_user_id(event)
        entry = MemoryEntry(
            memory_id=generate_memory_id(),
            owner_user_id=user_id,
            content=content.strip(),
            layer=layer,
            category=category,
            importance=importance,
            confidence=0.75,
            strength=0.8,
            stability=0.6,
            decay_rate=decay_rate_from_half_life(
                self.config.half_life_for_layer(layer)
            ),
            source="tool_call",
        )
        saved = await self.mem_repo.upsert(entry)
        return f"已记录 [{saved.memory_id}]"

    async def memory_update(self, event, memory_id="", content=""):
        if not self.config.enable_llm_tools:
            return "记忆工具已禁用"
        user_id = extract_user_id(event)
        entry = await self.mem_repo.get(memory_id, user_id)
        if not entry or not content.strip():
            return "未找到属于当前用户的记忆，或内容为空"
        entry.content = content.strip()
        entry.updated_at = entry.last_confirmed_at = utc_now()
        entry.source = "tool_call"
        await self.mem_repo.upsert(entry)
        return f"已更新 [{memory_id}]"

    async def memory_delete(self, event, memory_id=""):
        if not self.config.enable_llm_tools:
            return "记忆工具已禁用"
        user_id = extract_user_id(event)
        ok = await self.mem_repo.delete(memory_id, user_id)
        return f"已删除 [{memory_id}]" if ok else "未找到属于当前用户的记忆"
