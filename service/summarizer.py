import json
from typing import List, Tuple, Optional
from astrbot.api import logger
from astrbot.api.star import Context
from core.models import ConversationTurn, MemoryState, SummaryResult, SummaryOperation, MemoryEntry
from core.config import PluginConfig
from core.exceptions import SummaryError, ProviderNotFoundError
from utils.json_helper import safe_json_loads


class Summarizer:
    def __init__(self, config: PluginConfig, context: Context):
        self.config = config
        self.context = context

    async def _get_summary_provider(self):
        """获取用于总结的 LLM Provider"""
        provider_id = self.config.summary_provider_id.strip()
        if provider_id:
            try:
                provider = self.context.provider_manager.get_provider_by_id(provider_id)
                if provider:
                    return provider
            except Exception as e:
                logger.warning(f"指定的总结 Provider '{provider_id}' 获取失败: {e}，回退到主模型")
        return self.context.get_using_provider()

    async def summarize(
        self,
        turns: List[ConversationTurn],
        current_state: MemoryState,
        mode: str,
    ) -> SummaryResult:
        provider = await self._get_summary_provider()
        if not provider:
            raise ProviderNotFoundError("无法获取总结用 LLM Provider")

        prompt = self._build_prompt(turns, current_state, mode)
        system_prompt = self._build_system_prompt()

        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                session_id=None,
                contexts=[],
                image_urls=[],
                func_tool=None,
                system_prompt=system_prompt,
            )
            raw = llm_resp.completion_text
        except Exception as e:
            raise SummaryError(f"LLM 调用失败: {e}")

        parsed = safe_json_loads(raw)
        if not parsed:
            raise SummaryError(f"无法解析 LLM 返回的 JSON: {raw[:200]}")

        result = self._parse_result(parsed, mode)

        # 校验
        if mode == "full_replace" and result.full_state:
            ok, msg = self._validate_state(current_state, result.full_state)
            if not ok:
                raise SummaryError(f"校验失败: {msg}")
        elif mode == "search_replace":
            ok, msg = self._validate_operations(current_state, result.operations)
            if not ok:
                raise SummaryError(f"校验失败: {msg}")

        return result

    def _build_system_prompt(self) -> str:
        base = (
            "你是一个结构化记忆管理助手。你的任务是根据对话历史和现有记忆，"
            "生成精准的记忆更新操作。你必须只输出 JSON，不要有任何额外解释。"
        )
        if self.config.summary_system_prompt:
            base += f"\n\n{self.config.summary_system_prompt}"
        return base

    def _build_prompt(self, turns: List[ConversationTurn], state: MemoryState, mode: str) -> str:
        conversation_text = "\n".join(t.to_prompt_text() for t in turns)
        memory_snapshot = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)

        if mode == "search_replace":
            return self._search_replace_prompt(conversation_text, memory_snapshot)
        else:
            return self._full_replace_prompt(conversation_text, memory_snapshot)

    def _default_search_replace_prompt(self) -> str:
        return (
            "你就是这个对话中的 AI 助手（bot）。请站在你自己的视角，根据 [近期对话] 和 [你对当前用户的现有记忆]，"
            "更新你对这个用户的记忆。\n\n"
            "视角要求：\n"
            "- 使用第一人称视角，例如『用户喜欢...』『用户告诉我...』\n"
            "- 你是记忆的拥有者，这些记忆帮助你更好地与用户互动\n"
            "- 不要以第三者旁观者的口吻描述\n\n"
            "各层写入标准：\n"
            "- important：仅存放核心事实。如用户身份、关键偏好、重要约定、长期目标。"
            "必须有明确证据且对用户画像/互动方式有长期影响。\n"
            "- general：存放普通事实和常规互动。如日常爱好、一般性陈述、普通事件。\n"
            "- fleeting：只允许 add，不允许 update/delete/keep。"
            "尽可能详细记录近期对话中有用的信息（如临时任务、当前话题、短期状态、用户刚提到的细节）。"
            "fleeting 记忆会在后续总结轮次中自动淘汰，因此只关注最新内容。\n\n"
            "跨用户记忆：\n"
            "- 如果对话中提及其他用户的信息，且你认为需要记录到该用户的记忆中，"
            "请使用 memory_read_user 工具先读取该用户的记忆，再决定如何更新\n"
            "- 注意：对记忆的修改是互斥的，同一时间只能有一个总结任务在修改某个用户的记忆\n\n"
            "规则：\n"
            "1. 只能返回 JSON，不要有任何额外解释\n"
            "2. 操作类型：add / update / delete / keep\n"
            "3. update 和 delete 必须引用准确的 memory_id\n"
            "4. 不要 hallucinate，没有明确证据不要添加记忆\n"
            "5. 如果某条现有记忆仍然准确且相关，使用 keep\n"
            "6. fleeting 层只允许 add 操作\n\n"
            "[近期对话]\n{conversation_text}\n\n"
            "[现有记忆]\n{memory_snapshot}\n\n"
            "输出格式：\n"
            "{\n"
            '  "mode": "search_replace",\n'
            '  "summary": "总结说明",\n'
            '  "operations": [\n'
            '    {"action": "add", "layer": "general", "content": "...", "category": "fact", "importance": 3},\n'
            '    {"action": "update", "memory_id": "mem-xxx", "content": "..."},\n'
            '    {"action": "delete", "memory_id": "mem-xxx"},\n'
            '    {"action": "keep", "memory_id": "mem-xxx"},\n'
            '    {"action": "add", "layer": "fleeting", "content": "详细记录近期有用信息...", "category": "fact", "importance": 2}\n'
            "  ]\n"
            "}"
        )

    def _default_full_replace_prompt(self) -> str:
        return (
            "你就是这个对话中的 AI 助手（bot）。请站在你自己的视角，根据 [近期对话] 和 [你对当前用户的现有记忆]，"
            "生成完整的新的三层记忆结构。\n\n"
            "视角要求：\n"
            "- 使用第一人称视角，例如『用户喜欢...』『用户告诉我...』\n"
            "- 你是记忆的拥有者，这些记忆帮助你更好地与用户互动\n"
            "- 不要以第三者旁观者的口吻描述\n\n"
            "各层写入标准：\n"
            "- important：仅存放核心事实。如用户身份、关键偏好、重要约定、长期目标。"
            "必须有明确证据且对用户画像/互动方式有长期影响。\n"
            "- general：存放普通事实和常规互动。如日常爱好、一般性陈述、普通事件。\n"
            "- fleeting：只允许新增，不保留旧条目。"
            "尽可能详细记录近期对话中有用的信息（如临时任务、当前话题、短期状态、用户刚提到的细节）。"
            "fleeting 记忆会在后续总结轮次中自动淘汰，因此只关注最新内容。\n\n"
            "跨用户记忆：\n"
            "- 如果对话中提及其他用户的信息，且你认为需要记录到该用户的记忆中，"
            "请使用 memory_read_user 工具先读取该用户的记忆，再决定如何更新\n"
            "- 注意：对记忆的修改是互斥的，同一时间只能有一个总结任务在修改某个用户的记忆\n\n"
            "规则：\n"
            "1. 只能返回 JSON，不要有任何额外解释\n"
            "2. 不要遗漏现有记忆中仍然准确且重要的内容\n"
            "3. fleeting 层只保留本次需要新增的条目，不要复制旧 fleeting\n\n"
            "[近期对话]\n{conversation_text}\n\n"
            "[现有记忆]\n{memory_snapshot}\n\n"
            "输出格式：\n"
            "{\n"
            '  "mode": "full_replace",\n'
            '  "summary": "总结说明",\n'
            '  "full_state": {\n'
            '    "important": [{"memory_id": "...", "content": "...", "category": "fact", "importance": 5, ...}],\n'
            '    "general": [...],\n'
            '    "fleeting": [{"memory_id": "...", "content": "详细记录近期有用信息...", "category": "fact", "importance": 2, ...}]\n'
            "  }\n"
            "}"
        )

    def _search_replace_prompt(self, conversation_text: str, memory_snapshot: str) -> str:
        template = self.config.summary_search_replace_prompt
        if not template:
            template = self._default_search_replace_prompt()
        return template.format(
            conversation_text=conversation_text,
            memory_snapshot=memory_snapshot,
        )

    def _full_replace_prompt(self, conversation_text: str, memory_snapshot: str) -> str:
        template = self.config.summary_full_replace_prompt
        if not template:
            template = self._default_full_replace_prompt()
        return template.format(
            conversation_text=conversation_text,
            memory_snapshot=memory_snapshot,
        )

    def _parse_result(self, data: dict, mode: str) -> SummaryResult:
        return SummaryResult.from_dict(data, mode)

    def _validate_state(self, old: MemoryState, new: MemoryState) -> Tuple[bool, str]:
        # L1 记忆数量异常减少则拒绝
        old_important = len(old.important)
        new_important = len(new.important)
        if new_important < old_important * 0.5 and old_important > 0:
            return False, f"important 记忆数量异常减少: {old_important} -> {new_important}"
        return True, "ok"

    def _validate_operations(self, old: MemoryState, operations: List[SummaryOperation]) -> Tuple[bool, str]:
        all_ids = {e.memory_id for e in old.all_entries()}
        for op in operations:
            if op.action in ("update", "delete", "keep"):
                if not op.memory_id:
                    return False, f"{op.action} 操作缺少 memory_id"
                if op.memory_id not in all_ids:
                    return False, f"memory_id '{op.memory_id}' 不存在"
            if op.action == "add" and not op.content:
                return False, "add 操作缺少 content"
        return True, "ok"
