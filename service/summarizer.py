import json
import asyncio
from typing import List, Tuple, Optional, Dict
from astrbot.api import logger
from astrbot.api.star import Context
from core.models import ConversationTurn, MemoryState, SummaryResult, SummaryOperation, MemoryEntry
from core.config import PluginConfig
from core.exceptions import SummaryError, ProviderNotFoundError
from utils.json_helper import safe_json_loads
from utils.subject import build_cross_subject_id

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0


class Summarizer:
    def __init__(self, config: PluginConfig, context: Context):
        self.config = config
        self.context = context

    async def _get_summary_provider(self):
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
        subject_id: str,
        mem_repo,
        memory_mode: str,
    ) -> SummaryResult:
        provider = await self._get_summary_provider()
        if not provider:
            raise ProviderNotFoundError("无法获取总结用 LLM Provider")

        conversation_text = "\n".join(t.to_prompt_text() for t in turns)
        memory_snapshot = json.dumps(current_state.to_dict(), ensure_ascii=False, indent=2)
        system_prompt = self._build_system_prompt()

        turn1_prompt = self._build_turn1_prompt(conversation_text, memory_snapshot)
        raw_t1 = await self._call_llm_with_retry(provider, system_prompt, turn1_prompt, "T1")

        parsed = safe_json_loads(raw_t1)
        if not parsed:
            raise SummaryError(f"T1: 无法解析 LLM 返回的 JSON: {raw_t1[:300]}")

        action = parsed.get("action", "")
        if action == "read_users" and parsed.get("user_ids"):
            user_ids = parsed["user_ids"]
            logger.debug(f"总结器 T1 请求读取跨用户记忆: {user_ids}")

            cross_memories_text = await self._fetch_cross_user_memories(
                user_ids, subject_id, memory_mode, mem_repo
            )

            turn2_prompt = self._build_turn2_prompt(
                conversation_text, memory_snapshot, cross_memories_text
            )
            raw_t2 = await self._call_llm_with_retry(provider, system_prompt, turn2_prompt, "T2")

            parsed = safe_json_loads(raw_t2)
            if not parsed:
                raise SummaryError(f"T2: 无法解析 LLM 返回的 JSON: {raw_t2[:300]}")

        mode = parsed.get("mode", "search_replace")
        if mode not in ("search_replace", "full_replace"):
            mode = "search_replace"

        result = SummaryResult.from_dict(parsed, mode)

        if mode == "full_replace" and result.full_state:
            ok, msg = self._validate_state(current_state, result.full_state)
            if not ok:
                raise SummaryError(f"校验失败: {msg}")
        elif mode == "search_replace":
            ok, msg = self._validate_operations(current_state, result.operations)
            if not ok:
                raise SummaryError(f"校验失败: {msg}")

        if result.cross_user_operations:
            ok, msg = self._validate_cross_operations(result.cross_user_operations)
            if not ok:
                raise SummaryError(f"跨用户操作校验失败: {msg}")

        return result

    async def _call_llm_with_retry(self, provider, system_prompt: str, prompt: str, label: str) -> str:
        last_error = None
        current_prompt = prompt

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                llm_resp = await provider.text_chat(
                    prompt=current_prompt,
                    session_id=None,
                    contexts=[],
                    image_urls=[],
                    func_tool=None,
                    system_prompt=system_prompt,
                )
                raw = llm_resp.completion_text or ""

                parsed = safe_json_loads(raw)
                if parsed is not None:
                    return raw

                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        f"[{label}] 第 {attempt} 次 JSON 解析失败，{delay}s 后重试"
                    )
                    current_prompt = (
                        prompt
                        + f"\n\n[上一轮你的输出无法解析为 JSON，请严格只输出纯 JSON。你的错误输出: {raw[:500]}]"
                    )
                    await asyncio.sleep(delay)
                else:
                    last_error = f"JSON 解析失败，已重试 {MAX_RETRIES} 次"
            except Exception as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(f"[{label}] 第 {attempt} 次 LLM 调用异常: {e}，{delay}s 后重试")
                    await asyncio.sleep(delay)

        raise SummaryError(f"[{label}] LLM 调用失败: {last_error}")

    async def _fetch_cross_user_memories(
        self, user_ids: List[str], current_subject_id: str, memory_mode: str, mem_repo
    ) -> str:
        parts = []
        for uid in user_ids:
            target_subject = self._build_cross_subject_id(uid, current_subject_id, memory_mode)
            entries = await mem_repo.get_by_subject(target_subject)
            if entries:
                parts.append(f"=== 用户 {uid} 的现有记忆 (subject: {target_subject}) ===")
                for e in entries:
                    parts.append(
                        f"[{e.memory_id}] layer={e.layer} importance={e.importance} {e.content}"
                    )
                parts.append("")
            else:
                parts.append(f"=== 用户 {uid} 的现有记忆 (subject: {target_subject}) ===\n(无已有记忆)\n")
        return "\n".join(parts)

    def _build_cross_subject_id(self, user_id: str, current_subject_id: str, memory_mode: str) -> str:
        return build_cross_subject_id(user_id, current_subject_id, memory_mode)

    def _build_turn1_prompt(self, conversation_text: str, memory_snapshot: str) -> str:
        template = (
            "你就是这个对话中的 AI 助手（bot）。请站在你自己的视角，根据 [近期对话] 和 [你对当前用户的现有记忆]，"
            "更新你对这个用户的记忆。\n\n"
            "这是一个两阶段的记忆更新流程。你现在在阶段 1。\n\n"
            "阶段 1 任务：\n"
            "1. 分析对话中是否提及其他用户，且这些用户的信息变化需要记录到他们的记忆中\n"
            "2. 如果存在这样的跨用户记忆需求，请输出 read_users 请求来读取那些用户的现有记忆\n"
            "3. 如果不需要跨用户操作，直接输出最终结果\n\n"
            "阶段 1 输出格式（需要读取其他用户）：\n"
            "{\n"
            '  "action": "read_users",\n'
            '  "user_ids": ["user_id_1", "user_id_2"],\n'
            '  "reason": "简要说明为什么需要读取这些用户的记忆"\n'
            "}\n\n"
            "阶段 1 输出格式（不需要跨用户，直接输出结果）：\n"
            "{\n"
            '  "mode": "search_replace",\n'
            '  "summary": "总结说明",\n'
            '  "operations": [...],\n'
            '  "cross_user_operations": {}\n'
            "}\n\n"
            "视角要求：\n"
            "- 使用第一人称视角，例如『用户喜欢...』『用户告诉我...』\n"
            "- 当记忆内容涉及特定用户时，使用 {{uid:用户ID}} 格式引用，如『{{uid:12345}}和用户是好朋友』\n\n"
            "更新模式（自行选择）：\n"
            "- search_replace：精准修改。适用于大部分情况，对现有记忆进行增删改查。\n"
            "- full_replace：全量覆盖。适用于现有记忆已严重过时、需要完全重建时。\n\n"
            "各层写入标准：\n"
            "- important：仅存放核心事实。如用户身份、关键偏好、重要约定、长期目标。"
            "必须有明确证据且对用户画像/互动方式有长期影响。\n"
            "- general：存放普通事实和常规互动。如日常爱好、一般性陈述、普通事件。\n"
            "- fleeting：只允许 add，不允许 update/delete/keep。"
            "尽可能详细记录近期对话中有用的信息。fleeting 记忆会在后续自动淘汰。\n\n"
            "规则：\n"
            "1. 只能返回 JSON，不要有任何额外解释\n"
            "2. update 和 delete 必须引用准确的 memory_id\n"
            "3. 不要 hallucinate，没有明确证据不要添加记忆\n"
            "4. fleeting 层只允许 add 操作\n"
            "5. 跨用户操作在 cross_user_operations 中，key 为 user_id，value 为操作数组\n\n"
            "[近期对话]\n__CONVERSATION_TEXT__\n\n"
            "[现有记忆]\n__MEMORY_SNAPSHOT__\n"
        )
        template = template.replace("__CONVERSATION_TEXT__", conversation_text)
        template = template.replace("__MEMORY_SNAPSHOT__", memory_snapshot)
        return template

    def _build_turn2_prompt(
        self, conversation_text: str, memory_snapshot: str, cross_memories_text: str
    ) -> str:
        template = (
            "你就是这个对话中的 AI 助手（bot）。请站在你自己的视角，根据 [近期对话] 和 [你对当前用户的现有记忆]，"
            "更新你对这个用户的记忆。\n\n"
            "你现在在阶段 2。你已经读取了以下其他用户的记忆：\n\n"
            "__CROSS_MEMORIES__\n\n"
            "阶段 2 任务：\n"
            "根据所有信息，输出最终的记忆更新结果。包括：\n"
            "1. operations：对当前用户的记忆操作\n"
            "2. cross_user_operations：对其他用户的记忆操作（key 为 user_id，value 为操作数组）\n\n"
            "视角要求：\n"
            "- 使用第一人称视角，例如『用户喜欢...』『用户告诉我...』\n"
            "- 当记忆内容涉及特定用户时，使用 {{uid:用户ID}} 格式引用，如『{{uid:12345}}和用户是好朋友』\n\n"
            "更新模式（自行选择）：\n"
            "- search_replace：精准修改。适用于大部分情况，对现有记忆进行增删改查。\n"
            "- full_replace：全量覆盖。适用于现有记忆已严重过时、需要完全重建时。\n\n"
            "各层写入标准：\n"
            "- important：仅存放核心事实。必须有明确证据且对用户画像/互动方式有长期影响。\n"
            "- general：存放普通事实和常规互动。\n"
            "- fleeting：只允许 add。尽可能详细记录近期对话中有用的信息。\n\n"
            "规则：\n"
            "1. 只能返回 JSON，不要有任何额外解释\n"
            "2. update 和 delete 必须引用准确的 memory_id\n"
            "3. 不要 hallucinate，没有明确证据不要添加记忆\n"
            "4. fleeting 层只允许 add 操作\n\n"
            "[近期对话]\n__CONVERSATION_TEXT__\n\n"
            "[现有记忆]\n__MEMORY_SNAPSHOT__\n\n"
            "输出格式：\n"
            "{\n"
            '  "mode": "search_replace",\n'
            '  "summary": "总结说明",\n'
            '  "operations": [\n'
            '    {"action": "add|update|delete|keep", "layer": "important|general|fleeting", "content": "...", "category": "fact|profile|preference|task|event", "importance": 1-5, "memory_id": "..."},\n'
            "    ...\n"
            "  ],\n"
            '  "cross_user_operations": {\n'
            '    "user_id_1": [{"action": "add|update|delete", ...}],\n'
            '    "user_id_2": [{"action": "add", ...}]\n'
            "  }\n"
            "}"
        )
        template = template.replace("__CONVERSATION_TEXT__", conversation_text)
        template = template.replace("__MEMORY_SNAPSHOT__", memory_snapshot)
        template = template.replace("__CROSS_MEMORIES__", cross_memories_text)
        return template

    def _build_system_prompt(self) -> str:
        base = (
            "你是一个结构化记忆管理助手。你的任务是根据对话历史和现有记忆，"
            "生成精准的记忆更新操作。你必须只输出 JSON，不要有任何额外解释。"
        )
        if self.config.summary_system_prompt:
            base += f"\n\n{self.config.summary_system_prompt}"
        return base

    def _validate_state(self, old: MemoryState, new: MemoryState) -> Tuple[bool, str]:
        old_important = len(old.important)
        new_important = len(new.important)
        if new_important < old_important * 0.5 and old_important > 0:
            return False, f"important 记忆数量异常减少: {old_important} -> {new_important}"
        if not new.all_entries() and old.all_entries():
            return False, "full_replace 结果为空，拒绝应用"
        return True, "ok"

    def _validate_operations(self, state: MemoryState, operations: List[SummaryOperation]) -> Tuple[bool, str]:
        all_ids = {e.memory_id for e in state.all_entries()}
        for op in operations:
            if op.action in ("update", "delete", "keep"):
                if not op.memory_id:
                    return False, f"{op.action} 操作缺少 memory_id"
                if op.memory_id not in all_ids:
                    return False, f"memory_id '{op.memory_id}' 不存在于当前记忆中"
            if op.action == "add" and not op.content:
                return False, "add 操作缺少 content"
        return True, "ok"

    def _validate_cross_operations(self, cross_ops: Dict[str, List[SummaryOperation]]) -> Tuple[bool, str]:
        for uid, ops in cross_ops.items():
            if not uid or not uid.strip():
                return False, "cross_user_operations 中的 user_id 不能为空"
            for op in ops:
                if op.action == "add" and not op.content:
                    return False, f"跨用户 add 操作缺少 content（用户 {uid}）"
                if op.action in ("update", "delete", "keep") and not op.memory_id:
                    return False, f"跨用户 {op.action} 操作缺少 memory_id（用户 {uid}）"
        return True, "ok"

    async def condense(
        self,
        entries: List[MemoryEntry],
        target_count: int,
        layer: str,
    ) -> List[MemoryEntry]:
        provider = await self._get_summary_provider()
        if not provider:
            raise ProviderNotFoundError("无法获取浓缩用 LLM Provider")

        entries_json = json.dumps(
            [e.to_dict() for e in entries], ensure_ascii=False, indent=2
        )
        total_chars_before = sum(len(e.content) for e in entries)
        target_chars = int(total_chars_before * 0.65)
        target_ratio = 0.35

        base_system = (
            "你是一个记忆浓缩助手。你的任务是将多条记忆合并浓缩，"
            "删除冗余和不重要的内容，使总字符数至少减少 35%。你必须只输出 JSON。"
        )
        system_prompt = self._build_system_prompt() + "\n" + base_system

        prompt = self._build_condense_prompt(
            entries, target_count, layer, total_chars_before, target_chars
        )
        raw = await self._call_llm_with_retry(provider, system_prompt, prompt, "CONDENSE-R1")

        result = self._parse_condense_result(raw, layer, target_count)
        if result is not None:
            total_chars_after = sum(len(e.content) for e in result)
            ratio = (total_chars_before - total_chars_after) / total_chars_before if total_chars_before else 0
            if ratio >= target_ratio:
                logger.debug(
                    f"浓缩完成(R1): {layer} {len(entries)} -> {len(result)} 条, "
                    f"减少 {ratio:.1%}"
                )
                return result

        logger.debug(f"浓缩 R1 未达标，启动 R2 重试")

        retry_prompt = self._build_condense_retry_prompt(
            entries, target_count, layer, total_chars_before, target_chars, raw
        )
        raw2 = await self._call_llm_with_retry(provider, system_prompt, retry_prompt, "CONDENSE-R2")

        result = self._parse_condense_result(raw2, layer, target_count)
        if result is not None:
            total_chars_after = sum(len(e.content) for e in result)
            ratio = (total_chars_before - total_chars_after) / total_chars_before if total_chars_before else 0
            if ratio >= target_ratio:
                logger.debug(
                    f"浓缩完成(R2): {layer} {len(entries)} -> {len(result)} 条, "
                    f"减少 {ratio:.1%}"
                )
                return result

        logger.debug(f"浓缩 R2 未达标，启动 R3 最终重试")

        retry_prompt3 = self._build_condense_retry_prompt(
            entries, target_count, layer, total_chars_before, target_chars, raw2
        )
        raw3 = await self._call_llm_with_retry(provider, system_prompt, retry_prompt3, "CONDENSE-R3")

        result = self._parse_condense_result(raw3, layer, target_count)
        if result is not None:
            total_chars_after = sum(len(e.content) for e in result)
            ratio = (total_chars_before - total_chars_after) / total_chars_before if total_chars_before else 0
            logger.debug(
                f"浓缩完成(R3): {layer} {len(entries)} -> {len(result)} 条, "
                f"减少 {ratio:.1%}"
            )
            return result

        raise SummaryError(
            f"浓缩 3 轮仍未达到 35% 压缩目标 ({layer} 层 {len(entries)} 条)"
        )

    def _build_condense_prompt(
        self, entries, target_count: int, layer: str, total_before: int, target_after: int
    ) -> str:
        entries_json = json.dumps(
            [e.to_dict() for e in entries], ensure_ascii=False, indent=2
        )
        return (
            f"你需要将以下 [{layer}] 层的 {len(entries)} 条记忆压缩到最多 {target_count} 条，"
            f"且总字符数必须从当前的 {total_before} 减少到 {target_after} 以内（削减 35%）。\n\n"
            "你的策略：\n"
            "1. 合并语义相似、主题相同、或同一件事的多次记录\n"
            "2. 剔除低重要性(importance<=2)且信息冗余的记忆，仅保留核心要点\n"
            "3. 将多条同类事实提炼为一条精炼的总结\n"
            "4. 去除修饰性词语，使用简洁表述\n"
            "5. 优先保留高 importance 的记忆\n\n"
            f"[原始记忆]\n{entries_json}\n\n"
            "输出格式：\n"
            "{\n"
            "  \"condensed\": [\n"
            "    {\"memory_id\": \"保留原ID或生成新ID\", \"content\": \"...\", "
            "\"layer\": \"" + layer + "\", \"category\": \"fact\", \"importance\": 3},\n"
            "    ...\n"
            "  ]\n"
            "}"
        )

    def _build_condense_retry_prompt(
        self, entries, target_count: int, layer: str, total_before: int, target_after: int,
        last_raw: str
    ) -> str:
        entries_json = json.dumps(
            [e.to_dict() for e in entries], ensure_ascii=False, indent=2
        )
        return (
            f"你上一次的输出没有达到压缩目标。请重新处理。\n\n"
            f"目标: [{layer}] 层 {len(entries)} 条 → 最多 {target_count} 条，"
            f"总字符数 {total_before} → {target_after} 以内（削减 35%）。\n\n"
            f"你上一次的输出是:\n{last_raw[:800]}\n\n"
            "请更加激进地压缩：剔除冗余、合并同类、精简措辞。"
            "对于 importance<=2 且内容重复的，可以直接丢弃。\n\n"
            f"[原始记忆]\n{entries_json}\n\n"
            "输出格式：\n"
            "{\n"
            "  \"condensed\": [\n"
            "    {\"memory_id\": \"...\", \"content\": \"...\", "
            "\"layer\": \"" + layer + "\", \"category\": \"fact\", \"importance\": 3},\n"
            "    ...\n"
            "  ]\n"
            "}"
        )

    def _parse_condense_result(
        self, raw: str, layer: str, target_count: int
    ) -> Optional[List[MemoryEntry]]:
        parsed = safe_json_loads(raw)
        if not parsed:
            logger.warning(f"浓缩 JSON 解析失败: {raw[:200]}")
            return None

        condensed_data = parsed.get("condensed", [])
        if not condensed_data:
            logger.warning("浓缩结果为空")
            return None

        result = []
        for item in condensed_data:
            entry = MemoryEntry(
                memory_id=item.get("memory_id", ""),
                content=item.get("content", ""),
                layer=layer,
                category=item.get("category", "fact"),
                importance=min(max(item.get("importance", 3), 1), 5),
            )
            result.append(entry)

        if len(result) > target_count:
            logger.warning(f"浓缩结果 {len(result)} 条仍超出限制 {target_count}，截断")
            result.sort(key=lambda e: (e.importance, e.content))
            result = result[:target_count]

        return result
