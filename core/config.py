from dataclasses import dataclass
from typing import Literal


@dataclass
class PluginConfig:
    memory_mode: Literal["global", "shared"] = "global"
    fifo_size: int = 10
    inject_fifo_in_group: bool = True
    max_memory_per_layer: int = 50
    fleeting_ttl_rounds: int = 3
    max_concurrent_summaries: int = 2

    # LLM Provider 配置
    summary_provider_id: str = ""
    summary_system_prompt: str = ""

    # 总结提示词模板（留空则使用默认提示词）
    summary_search_replace_prompt: str = ""
    summary_full_replace_prompt: str = ""

    # 注入控制
    inject_memory_in_private: bool = True
    inject_layers_in_group: Literal["important_only", "important_general", "all"] = "important_only"

    # 溢出处理
    memory_overflow_policy: Literal["evict", "condense"] = "evict"

    # 功能开关
    enable_auto_summary: bool = True
    enable_manual_summary: bool = True
    enable_llm_tools: bool = True
    tool_caution_in_prompt: bool = True

    @classmethod
    def from_astrbot_config(cls, config: dict) -> "PluginConfig":
        return cls(
            memory_mode=config.get("memory_mode", "global"),
            fifo_size=config.get("fifo_size", 10),
            inject_fifo_in_group=config.get("inject_fifo_in_group", True),
            max_memory_per_layer=config.get("max_memory_per_layer", 50),
            fleeting_ttl_rounds=config.get("fleeting_ttl_rounds", 3),
            max_concurrent_summaries=config.get("max_concurrent_summaries", 2),
            summary_provider_id=config.get("summary_provider_id", ""),
            summary_system_prompt=config.get("summary_system_prompt", ""),
            summary_search_replace_prompt=config.get("summary_search_replace_prompt", ""),
            summary_full_replace_prompt=config.get("summary_full_replace_prompt", ""),
            inject_memory_in_private=config.get("inject_memory_in_private", True),
            inject_layers_in_group=config.get("inject_layers_in_group", "important_only"),
            memory_overflow_policy=config.get("memory_overflow_policy", "evict"),
            enable_auto_summary=config.get("enable_auto_summary", True),
            enable_manual_summary=config.get("enable_manual_summary", True),
            enable_llm_tools=config.get("enable_llm_tools", True),
            tool_caution_in_prompt=config.get("tool_caution_in_prompt", True),
        )
