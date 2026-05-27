import re
from typing import List, Optional, Dict
from core.models import MemoryState, MemoryEntry, ConversationTurn
from core.config import PluginConfig

_UID_PATTERN = re.compile(r"\{\{uid:(\S+?)\}\}")


class Injector:
    def __init__(self, config: PluginConfig):
        self.config = config
        self.nickname_cache: Dict[str, str] = {}

    def update_nickname_cache(self, cache: Dict[str, str]) -> None:
        self.nickname_cache = cache

    def build_memory_prompt(
        self,
        state: MemoryState,
        subject_id: str,
        scene: str,
        fifo_turns: Optional[List[ConversationTurn]] = None,
    ) -> str:
        parts = []
        parts.append("\n\n====================")
        parts.append("### [MEMORY SYSTEM] ###")
        parts.append(f"- Current subject_id: {subject_id}")
        parts.append(f"- Scene: {scene}")
        parts.append("")

        if scene == "private" and self.config.inject_memory_in_private:
            parts.append(self._format_layer("important", state.important))
            parts.append(self._format_layer("general", state.general))
            parts.append(self._format_layer("fleeting", state.fleeting))
        elif scene == "group":
            layers = self.config.inject_layers_in_group
            parts.append(self._format_layer("important", state.important))
            if layers in ("important_general", "all"):
                parts.append(self._format_layer("general", state.general))
            if layers == "all":
                parts.append(self._format_layer("fleeting", state.fleeting))

            if self.config.inject_fifo_in_group and fifo_turns:
                parts.append("### [RECENT CONVERSATION WITH YOU] ###")
                for turn in fifo_turns:
                    parts.append(self._resolve_uid(turn.to_prompt_text()))
                parts.append("")

        parts.append("### [MEMORY RULES] ###")
        parts.append("1. 只能将标记为当前 subject_id 的记忆应用到当前用户")
        parts.append("2. important 层是核心画像，general 是普通事实，fleeting 是临时内容")
        parts.append("3. 不要张冠李戴，未标记的记忆不要强行关联")
        uid_list = [uid for uid in self.nickname_cache if uid]
        if uid_list:
            uid_example = ", ".join(
                f"{{{{uid:{uid}}}}}={{{self.nickname_cache.get(uid, uid)}}}"
                for uid in uid_list[:5]
            )
            parts.append(f"4. 记忆中的 {{{{uid:xxx}}}} 已自动替换为当前昵称 ({uid_example}...)")
        parts.append("====================\n")

        return "\n".join(parts)

    def _format_layer(self, name: str, entries: List[MemoryEntry]) -> str:
        if not entries:
            return f"<{name}>\n(No entries)\n</{name}>\n"
        lines = [f"<{name}>"]
        for e in entries:
            resolved_content = self._resolve_uid(e.content)
            lines.append(f"  - [{e.memory_id}] {resolved_content} (importance: {e.importance})")
        lines.append(f"</{name}>\n")
        return "\n".join(lines)

    def _resolve_uid(self, text: str) -> str:
        if "{{uid:" not in text:
            return text

        def replacer(match):
            uid = match.group(1)
            name = self.nickname_cache.get(uid)
            if name:
                return name
            return uid

        return _UID_PATTERN.sub(replacer, text)

    def build_active_users_section(self, users_data: list) -> str:
        if not users_data:
            return ""
        parts = ["### [ACTIVE USERS' IMPRESSIONS] ###"]
        for item in users_data:
            user_label = self._resolve_uid(item.get("user_label", item.get("user_id", "?")))
            parts.append(f"--- {user_label} ---")
            for layer_name, entries in item.get("layers", {}).items():
                if entries:
                    for e in entries:
                        parts.append(f"  [{layer_name}] {self._resolve_uid(e.content)}")
        parts.append("")
        return "\n".join(parts)
