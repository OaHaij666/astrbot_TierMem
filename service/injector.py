from typing import Dict, List, Optional

from core.models import ConversationTurn, Entity, MemoryEntry, Relation


class Injector:
    def __init__(self, config):
        self.config = config
        self.nickname_cache: Dict[str, str] = {}

    def update_nickname_cache(self, cache):
        self.nickname_cache = cache

    def build_prompt(
        self,
        user_id: str,
        scene: str,
        memories: List[MemoryEntry],
        relations: List[Relation],
        entities: Dict[str, Entity],
        fifo_turns: Optional[List[ConversationTurn]] = None,
    ) -> str:
        parts = [
            "\n\n### [TIERMEM CONTEXT]",
            f"current_user=user:{user_id}",
            f"scene={scene}",
        ]
        if memories:
            parts.append("\n[ATOMIC MEMORIES]")
            for m in memories:
                parts.append(
                    f"- [{m.layer}] {m.content} "
                    f"(confidence={m.confidence:.2f}, strength={m.effective_strength():.2f})"
                )
        if relations:
            parts.append("\n[KNOWLEDGE GRAPH: ONE-HOP RELATIONS]")
            for r in relations:
                source = self._entity_label(r.source_entity_id, entities)
                target = self._entity_label(r.target_entity_id, entities)
                parts.append(
                    f"- {source} --{r.relation_type}--> {target} "
                    f"(confidence={r.confidence:.2f}, strength={r.effective_strength():.2f})"
                )
        if scene == "group" and fifo_turns:
            parts.append("\n[RECENT UNSUMMARIZED TURNS]")
            parts.extend(t.to_prompt_text() for t in fifo_turns)
        parts.extend(
            [
                "\n[RULES]",
                "- 原子记忆只适用于 current_user；图谱关系只表示实体间关系。",
                "- strength 很低的信息只能作为弱提示；不得把推测当事实。",
                "- 不要向用户泄露内部 memory_id、置信度或存储结构。",
                "### [/TIERMEM CONTEXT]\n",
            ]
        )
        return "\n".join(parts)

    def _entity_label(self, entity_id: str, entities: Dict[str, Entity]) -> str:
        entity = entities.get(entity_id)
        if entity:
            return f"{entity.name}<{entity.entity_id}>"
        if entity_id.startswith("user:"):
            uid = entity_id[5:]
            return f"{self.nickname_cache.get(uid, uid)}<{entity_id}>"
        return entity_id
