import asyncio
import json
from typing import List

from astrbot.api import logger
from astrbot.api.star import Context

if __package__ and "." in __package__:
    from ..core.config import PluginConfig
    from ..core.exceptions import ProviderNotFoundError, SummaryError
    from ..core.models import ConversationTurn, MemoryEntry, Relation, SummaryResult
    from ..utils.json_helper import safe_json_loads
else:
    from core.config import PluginConfig
    from core.exceptions import ProviderNotFoundError, SummaryError
    from core.models import ConversationTurn, MemoryEntry, Relation, SummaryResult
    from utils.json_helper import safe_json_loads


class Summarizer:
    def __init__(self, config: PluginConfig, context: Context):
        self.config, self.context = config, context

    async def _provider(self):
        provider_id = self.config.summary_provider_id.strip()
        if provider_id:
            try:
                provider = self.context.provider_manager.get_provider_by_id(provider_id)
                if provider:
                    return provider
            except Exception as exc:
                logger.warning(f"总结 Provider {provider_id} 不可用，回退主模型: {exc}")
        return self.context.get_using_provider()

    async def summarize(
        self,
        turns: List[ConversationTurn],
        memories: List[MemoryEntry],
        relations: List[Relation],
        user_id: str,
        context_id: str,
    ) -> SummaryResult:
        provider = await self._provider()
        if not provider:
            raise ProviderNotFoundError("无法获取总结模型")
        prompt = self._build_prompt(turns, memories, relations, user_id, context_id)
        raw = await self._call(provider, self._system_prompt(), prompt)
        parsed = safe_json_loads(raw)
        if not isinstance(parsed, dict):
            raise SummaryError(f"总结结果不是 JSON 对象: {raw[:300]}")
        result = SummaryResult.from_dict(parsed)
        self._validate(result, memories, relations, user_id)
        return result

    async def _call(self, provider, system_prompt: str, prompt: str) -> str:
        last_error = ""
        current = prompt
        for attempt in range(3):
            try:
                response = await provider.text_chat(
                    prompt=current,
                    session_id=None,
                    contexts=[],
                    image_urls=[],
                    func_tool=None,
                    system_prompt=system_prompt,
                )
                raw = response.completion_text or ""
                if isinstance(safe_json_loads(raw), dict):
                    return raw
                last_error = "JSON 解析失败"
                current = prompt + "\n\n上一输出无法解析。严格只输出一个 JSON 对象。"
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise SummaryError(f"总结调用失败: {last_error}")

    def _system_prompt(self) -> str:
        base = (
            "你是记忆与关系抽取器。个人记忆只描述当前用户自身；"
            "用户与其他实体之间的事实必须写成知识图谱关系。只输出 JSON，不得猜测。"
        )
        return base + (
            f"\n{self.config.summary_system_prompt}"
            if self.config.summary_system_prompt
            else ""
        )

    def _build_prompt(self, turns, memories, relations, user_id, context_id) -> str:
        memory_json = json.dumps(
            [m.to_dict() for m in memories], ensure_ascii=False, indent=2
        )
        relation_json = json.dumps(
            [r.__dict__ for r in relations], ensure_ascii=False, indent=2
        )
        conversation = "\n".join(t.to_prompt_text() for t in turns)
        return f"""
当前用户实体: user:{user_id}
当前上下文: {context_id}

[近期对话]
{conversation}

[当前用户已有原子记忆]
{memory_json}

[当前用户一跳关系]
{relation_json}

请输出：
{{
  "summary": "简述本次变化",
  "memory_operations": [
    {{"action":"add|update|delete|reinforce", "memory_id":"更新时必填", "content":"原子事实",
      "layer":"core|semantic|episodic|working", "category":"profile|preference|task|fact|event",
      "importance":1, "confidence":0.0, "stability":0.0, "visibility_scope":"private|group|public",
      "entity_ids":["与该事实直接相关的已有实体 ID"]}}
  ],
  "relation_operations": [
    {{"action":"add|update|delete|reinforce", "relation_id":"更新时必填",
      "source_entity_id":"user:{user_id}", "source_entity_type":"user", "source_name":"名称", "source_aliases":["别名"],
      "relation_type":"friend_of|colleague_of|member_of|participates_in|likes|其他简短谓词",
      "target_entity_id":"user:ID 或 project:稳定标识", "target_entity_type":"user|group|project|organization|topic|other",
      "target_name":"名称", "target_aliases":["别名"], "confidence":0.0, "stability":0.0,
      "visibility_scope":"private|group|public", "evidence":"对话中的直接证据"}}
  ]
}}

规则：
1. 一条 memory 只表达一个事实；关系不要复制进双方个人记忆。
2. core 只放长期稳定身份/偏好；semantic 放一般事实；episodic 放带时间事件；working 放短期事项。
3. 同义事实使用 reinforce 或 update，不重复 add；矛盾事实 delete 旧项并 add 新项。
4. 关系必须有证据，实体 ID 必须带类型前缀。无法确定用户 ID 时不要创建用户关系。
5. group 可见信息的 visibility_scope 使用 group；敏感信息使用 private。
6. 非关系原子若明确提到当前图中的实体，在 entity_ids 中列出；不要虚构不存在的实体 ID。
7. category=relation 由系统从关系 evidence 自动生成，不要在 memory_operations 中手工创建。
""".strip()

    def _validate(self, result, memories, relations, user_id):
        memory_ids = {m.memory_id for m in memories}
        relation_ids = {r.relation_id for r in relations}
        known_entity_ids = {
            f"user:{user_id}",
            *(
                entity_id
                for relation in relations
                for entity_id in (
                    relation.source_entity_id,
                    relation.target_entity_id,
                )
            ),
        }
        for op in result.memory_operations:
            if op.action not in ("add", "update", "delete", "reinforce"):
                raise SummaryError(f"非法记忆操作: {op.action}")
            if op.action != "add" and op.memory_id not in memory_ids:
                raise SummaryError(f"记忆不属于当前用户: {op.memory_id}")
            if op.action in ("add", "update"):
                if not op.content or op.layer not in (
                    "core",
                    "semantic",
                    "episodic",
                    "working",
                ):
                    raise SummaryError("记忆内容或层级无效")
                if op.category not in (
                    "profile",
                    "preference",
                    "task",
                    "fact",
                    "event",
                ):
                    raise SummaryError("记忆分类无效")
                if any(
                    entity_id not in known_entity_ids for entity_id in op.entity_ids
                ):
                    raise SummaryError("记忆引用了当前邻域之外的实体")
        anchor = f"user:{user_id}"
        for op in result.relation_operations:
            if op.action not in ("add", "update", "delete", "reinforce"):
                raise SummaryError(f"非法关系操作: {op.action}")
            if op.action != "add" and op.relation_id not in relation_ids:
                raise SummaryError(f"关系不在当前邻域: {op.relation_id}")
            if op.action in ("update", "delete"):
                existing = next(r for r in relations if r.relation_id == op.relation_id)
                if existing.owner_user_id and existing.owner_user_id != user_id:
                    raise SummaryError("不能修改或删除其他用户拥有的关系")
            if op.action == "add":
                if (
                    not op.relation_type
                    or not op.source_entity_id
                    or not op.target_entity_id
                    or not op.evidence
                ):
                    raise SummaryError("新增关系缺少实体、类型或证据")
                if anchor not in (op.source_entity_id, op.target_entity_id):
                    raise SummaryError("新增关系必须连接当前用户")
