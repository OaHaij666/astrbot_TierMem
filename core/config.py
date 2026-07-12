from dataclasses import dataclass, field


def default_relation_intent_keywords():
    return {
        "friend_of": ["朋友", "好友", "友人", "friend"],
        "colleague_of": ["同事", "同僚", "共事", "colleague"],
        "participates_in": ["参与", "负责", "开发", "项目", "participate"],
        "member_of": ["成员", "加入", "属于", "组织", "member"],
        "likes": ["喜欢", "爱好", "感兴趣", "like"],
        "family_of": ["家人", "亲属", "父亲", "母亲", "兄弟", "姐妹", "family"],
    }


@dataclass
class PluginConfig:
    fifo_size: int = 10
    fifo_max_wait_minutes: float = 30.0
    max_memories_per_user: int = 200
    max_injected_memories: int = 24
    max_injected_relations: int = 12
    atom_fts_candidate_limit: int = 40
    atom_like_candidate_limit: int = 24
    atom_background_limit: int = 4
    atom_query_term_limit: int = 24
    graph_recall_max_hops: int = 2
    graph_alias_min_length: int = 2
    graph_max_matched_entities: int = 6
    graph_entity_scan_limit: int = 5000
    relation_intent_keywords: dict = field(
        default_factory=default_relation_intent_keywords
    )
    max_concurrent_summaries: int = 2

    summary_provider_id: str = ""
    summary_system_prompt: str = ""

    inject_memory_in_private: bool = True
    inject_memory_in_group: bool = True
    inject_fifo_in_group: bool = True

    enable_auto_summary: bool = True
    enable_manual_summary: bool = True
    enable_llm_tools: bool = True
    tool_caution_in_prompt: bool = True

    # 各层默认半衰期（天）。core 为 0，表示不随时间自动衰减。
    core_half_life_days: float = 0.0
    semantic_half_life_days: float = 180.0
    episodic_half_life_days: float = 45.0
    working_half_life_days: float = 7.0
    relation_half_life_days: float = 180.0
    retrieval_min_strength: float = 0.08

    @classmethod
    def from_astrbot_config(cls, config: dict) -> "PluginConfig":
        values = {}
        for name in cls.__dataclass_fields__:
            if name in config:
                values[name] = config[name]
        return cls(**values)

    def half_life_for_layer(self, layer: str) -> float:
        return {
            "core": self.core_half_life_days,
            "semantic": self.semantic_half_life_days,
            "episodic": self.episodic_half_life_days,
            "working": self.working_half_life_days,
        }.get(layer, self.semantic_half_life_days)
