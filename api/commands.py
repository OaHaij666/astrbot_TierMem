if __package__ and "." in __package__:
    from ..core.config import PluginConfig
    from ..storage.fifo_repo import FifoRepository
    from ..storage.graph_repo import GraphRepository
    from ..storage.memory_repo import MemoryRepository
    from ..utils.subject import extract_context_id, extract_user_id
else:
    from core.config import PluginConfig
    from storage.fifo_repo import FifoRepository
    from storage.graph_repo import GraphRepository
    from storage.memory_repo import MemoryRepository
    from utils.subject import extract_context_id, extract_user_id


class CommandHandler:
    def __init__(
        self,
        config: PluginConfig,
        mem_repo: MemoryRepository,
        fifo_repo: FifoRepository,
        graph_repo: GraphRepository,
    ):
        self.config, self.mem_repo = config, mem_repo
        self.fifo_repo, self.graph_repo = fifo_repo, graph_repo

    async def check(self, event, layer=None):
        if layer and layer not in ("core", "semantic", "episodic", "working"):
            return event.plain_result("层级应为 core/semantic/episodic/working")
        entries = await self.mem_repo.get_by_user(extract_user_id(event), layer)
        if not entries:
            return event.plain_result("当前无原子记忆。")
        lines = [f"=== {layer or '全部'}原子记忆 ==="]
        for e in entries:
            lines.append(
                f"[{e.layer}] {e.content[:100]} (强度 {e.effective_strength():.2f}, id {e.memory_id})"
            )
        return event.plain_result("\n".join(lines))

    async def graph(self, event):
        user_id, context_id = extract_user_id(event), extract_context_id(event)
        relations = await self.graph_repo.get_neighbors(
            f"user:{user_id}", self.config.max_injected_relations, 0.0, context_id
        )
        if not relations:
            return event.plain_result("当前用户暂无知识图谱关系。")
        lines = ["=== 一跳关系 ==="]
        for r in relations:
            lines.append(
                f"{r.source_entity_id} --{r.relation_type}--> {r.target_entity_id} "
                f"(强度 {r.effective_strength():.2f})"
            )
        return event.plain_result("\n".join(lines))

    async def status(self, event):
        user_id = extract_user_id(event)
        counts = {
            layer: await self.mem_repo.count_by_user_layer(user_id, layer)
            for layer in ("core", "semantic", "episodic", "working")
        }
        relations = await self.graph_repo.get_neighbors(
            f"user:{user_id}", 9999, 0.0, extract_context_id(event)
        )
        fifo = await self.fifo_repo.count(user_id, extract_context_id(event))
        return event.plain_result(
            "=== TierMem 状态 ===\n"
            + "\n".join(f"{k}: {v}" for k, v in counts.items())
            + f"\nrelations: {len(relations)}\nFIFO: {fifo}/{self.config.fifo_size}"
        )

    async def fifo(self, event):
        turns = await self.fifo_repo.get_turns(
            extract_user_id(event), self.config.fifo_size, extract_context_id(event)
        )
        if not turns:
            return event.plain_result("FIFO 为空。")
        lines = ["=== FIFO ==="]
        for i, turn in enumerate(turns, 1):
            lines.append(
                f"{i}. 用户: {turn.user_message[:100]}\n   助手: {turn.assistant_message[:100]}"
            )
        return event.plain_result("\n".join(lines))

    async def clear(self, event):
        user_id = extract_user_id(event)
        await self.mem_repo.delete_by_user(user_id)
        await self.fifo_repo.clear(user_id)
        await self.graph_repo.clear_user_graph(user_id)
        return event.plain_result("已清除你的原子记忆、FIFO 和关联关系。")

    def help(self, event):
        return event.plain_result(
            "/memory sum - 立即总结\n"
            "/memory check [core|semantic|episodic|working] - 查看原子记忆\n"
            "/memory graph - 查看一跳关系\n"
            "/memory fifo - 查看近期缓存\n"
            "/memory status - 查看状态\n"
            "/memory clear - 清除自己的记忆与关系\n"
            "/memory rollback - 管理员回滚整库\n"
            "/memory help - 帮助"
        )
