import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.models import Entity, Relation, RelationEvidence
from storage.memory_repo import AtomSearchResult


_UID_PATTERN = re.compile(r"\{\{uid:([^}]+)\}\}|@([A-Za-z0-9_-]{3,})")


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


@dataclass
class EntityMatch:
    entity: Entity
    alias: str
    match_kind: str
    score: float
    memory_ids: List[str] = field(default_factory=list)


@dataclass
class EvidenceEdgeMatch:
    memory_id: str
    relation: Relation
    evidence: RelationEvidence


@dataclass
class ScoredRelation:
    relation: Relation
    score: float
    reasons: List[str] = field(default_factory=list)
    hop: int = 1
    supporting_memory_ids: List[str] = field(default_factory=list)


@dataclass
class GraphRecallResult:
    matched_entities: List[EntityMatch] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)
    scored_relations: List[ScoredRelation] = field(default_factory=list)
    atom_search: Optional[AtomSearchResult] = None
    atom_entities: Dict[str, List[Entity]] = field(default_factory=dict)
    evidence_edges: List[EvidenceEdgeMatch] = field(default_factory=list)

    @property
    def relations(self):
        return [item.relation for item in self.scored_relations]

    @property
    def memories(self):
        return self.atom_search.memories if self.atom_search else []


class GraphRetriever:
    def __init__(self, config, graph_repo, memory_repo=None):
        self.config = config
        self.graph_repo = graph_repo
        self.memory_repo = memory_repo

    async def recall(
        self, user_id: str, message: str, context_id: str
    ) -> GraphRecallResult:
        atom_search = None
        if self.memory_repo is not None:
            atom_search = await self.memory_repo.search_atoms(
                message,
                user_id,
                limit=self.config.max_injected_memories,
                min_strength=self.config.retrieval_min_strength,
                fts_candidate_limit=getattr(
                    self.config, "atom_fts_candidate_limit", 40
                ),
                like_candidate_limit=getattr(
                    self.config, "atom_like_candidate_limit", 24
                ),
                background_limit=getattr(self.config, "atom_background_limit", 4),
                query_term_limit=getattr(self.config, "atom_query_term_limit", 24),
                context_id=context_id,
                additional_owner_ids=(
                    [context_id] if context_id.startswith("group:") else []
                ),
            )
        memory_ids = (
            [hit.memory.memory_id for hit in atom_search.hits] if atom_search else []
        )
        atom_entities = await self.graph_repo.get_entities_for_memories(memory_ids)
        evidence_rows = await self.graph_repo.get_evidence_relations_for_memories(
            memory_ids, user_id, context_id
        )
        evidence_edges = [
            EvidenceEdgeMatch(memory_id, relation, evidence)
            for memory_id, relation, evidence in evidence_rows
            if evidence.polarity == "support"
        ]

        entities = await self.graph_repo.list_entities(
            self.config.graph_entity_scan_limit
        )
        exact_matches = await self._match_entities(
            user_id, message, context_id, entities
        )
        atom_matches = self._atom_entity_matches(atom_search, atom_entities)
        matches = self._merge_matches(exact_matches + atom_matches)
        intents = self._match_intents(message)

        current = f"user:{user_id}"
        atom_entity_ids = {
            entity.entity_id
            for mapped_entities in atom_entities.values()
            for entity in mapped_entities
        }
        anchors = {
            current,
            *(match.entity.entity_id for match in matches),
            *atom_entity_ids,
        }
        if context_id.startswith("group:") and any(
            entity.entity_id == context_id for entity in entities
        ):
            anchors.add(context_id)

        candidate_limit = max(50, self.config.max_injected_relations * 10)
        first = await self.graph_repo.get_relations_touching(
            anchors,
            user_id,
            context_id,
            self.config.retrieval_min_strength,
            candidate_limit,
        )
        relations = {relation.relation_id: relation for relation in first}
        hop_by_relation = {relation.relation_id: 1 for relation in first}
        for edge in evidence_edges:
            relations[edge.relation.relation_id] = edge.relation
            hop_by_relation[edge.relation.relation_id] = 1

        if self.config.graph_recall_max_hops >= 2:
            frontier = {
                entity_id
                for relation in first
                for entity_id in (
                    relation.source_entity_id,
                    relation.target_entity_id,
                )
                if entity_id not in anchors
            }
            second = await self.graph_repo.get_relations_touching(
                frontier,
                user_id,
                context_id,
                self.config.retrieval_min_strength,
                candidate_limit,
            )
            for relation in second:
                relations.setdefault(relation.relation_id, relation)
                hop_by_relation.setdefault(relation.relation_id, 2)

        scored = self._score_relations(
            list(relations.values()),
            current,
            exact_matches,
            matches,
            intents,
            context_id,
            atom_search,
            atom_entities,
            evidence_edges,
            hop_by_relation,
        )
        return GraphRecallResult(
            matched_entities=matches,
            intents=intents,
            scored_relations=scored[: self.config.max_injected_relations],
            atom_search=atom_search,
            atom_entities=atom_entities,
            evidence_edges=evidence_edges,
        )

    def _atom_entity_matches(self, atom_search, atom_entities):
        if not atom_search:
            return []
        scores = {hit.memory.memory_id: hit.score for hit in atom_search.hits}
        result = []
        by_entity = {}
        for memory_id, entities in atom_entities.items():
            for entity in entities:
                by_entity.setdefault(entity.entity_id, (entity, []))[1].append(
                    memory_id
                )
        for entity, memory_ids in by_entity.values():
            score = 50.0 + 30.0 * max(
                scores.get(memory_id, 0.0) for memory_id in memory_ids
            )
            result.append(
                EntityMatch(entity, entity.name, "memory", round(score, 3), memory_ids)
            )
        return result

    def _merge_matches(self, matches):
        deduped = {}
        for match in matches:
            old = deduped.get(match.entity.entity_id)
            if old is None:
                deduped[match.entity.entity_id] = match
            elif match.score > old.score:
                match.memory_ids = list(
                    dict.fromkeys(old.memory_ids + match.memory_ids)
                )
                deduped[match.entity.entity_id] = match
            else:
                old.memory_ids = list(dict.fromkeys(old.memory_ids + match.memory_ids))
        return sorted(deduped.values(), key=lambda item: item.score, reverse=True)[
            : self.config.graph_max_matched_entities
        ]

    async def _match_entities(self, user_id, message, context_id, entities):
        normalized = normalize_text(message)
        by_id = {entity.entity_id: entity for entity in entities}
        direct_ids = {
            f"user:{left or right}"
            for left, right in _UID_PATTERN.findall(message)
            if left or right
        }
        raw = []
        for entity_id in direct_ids:
            if entity_id in by_id:
                raw.append(EntityMatch(by_id[entity_id], entity_id, "uid", 100.0))

        alias_map: Dict[str, List[Entity]] = {}
        for entity in entities:
            for alias in [entity.name, *entity.aliases]:
                key = normalize_text(alias).strip()
                if len(key) >= self.config.graph_alias_min_length:
                    alias_map.setdefault(key, []).append(entity)
        occupied = []
        for alias in sorted(alias_map, key=len, reverse=True):
            start = normalized.find(alias)
            if start < 0:
                continue
            span = (start, start + len(alias))
            if any(span[0] >= old[0] and span[1] <= old[1] for old in occupied):
                continue
            occupied.append(span)
            for entity in alias_map[alias]:
                if entity.entity_id != f"user:{user_id}":
                    raw.append(
                        EntityMatch(
                            entity,
                            alias,
                            "name" if normalize_text(entity.name) == alias else "alias",
                            80.0 if normalize_text(entity.name) == alias else 60.0,
                        )
                    )

        if raw:
            candidates = await self.graph_repo.get_relations_touching(
                [f"user:{user_id}"], user_id, context_id, 0.0
            )
            adjacent = {
                entity_id
                for relation in candidates
                for entity_id in (
                    relation.source_entity_id,
                    relation.target_entity_id,
                )
            }
            for match in raw:
                if match.entity.entity_id in adjacent:
                    match.score += 25
        return self._merge_matches(raw)

    def _match_intents(self, message: str):
        normalized = normalize_text(message)
        return [
            str(relation_type)
            for relation_type, keywords in self.config.relation_intent_keywords.items()
            if any(
                normalize_text(str(word)) in normalized
                for word in keywords
                if str(word)
            )
        ]

    def _score_relations(
        self,
        relations,
        current,
        exact_matches,
        matches,
        intents,
        context_id,
        atom_search,
        atom_entities,
        evidence_edges,
        hop_by_relation,
    ):
        exact = {match.entity.entity_id for match in exact_matches}
        anchors = {
            current,
            *(match.entity.entity_id for match in matches),
            *(
                entity.entity_id
                for mapped_entities in atom_entities.values()
                for entity in mapped_entities
            ),
        }
        atom_scores = (
            {hit.memory.memory_id: hit.score for hit in atom_search.hits}
            if atom_search
            else {}
        )
        memories_by_entity = {}
        for memory_id, entities in atom_entities.items():
            for entity in entities:
                memories_by_entity.setdefault(entity.entity_id, []).append(memory_id)
        evidence_by_relation = {}
        for edge in evidence_edges:
            evidence_by_relation.setdefault(edge.relation.relation_id, []).append(
                edge.memory_id
            )

        scored = []
        for relation in relations:
            ends = {relation.source_entity_id, relation.target_entity_id}
            support_ids = list(evidence_by_relation.get(relation.relation_id, []))
            for entity_id in ends:
                support_ids.extend(memories_by_entity.get(entity_id, []))
            support_ids = list(dict.fromkeys(support_ids))
            atom_support = max(
                (atom_scores.get(key, 0.0) for key in support_ids), default=0.0
            )
            hop = hop_by_relation.get(relation.relation_id, 2)
            reasons = []
            score = (
                0.35 * relation.effective_strength()
                + 0.15 * relation.confidence
                + 0.25 * atom_support
            )
            if relation.relation_id in evidence_by_relation:
                score += 0.20
                reasons.append("召回原子直接支持此关系")
            if ends & exact:
                score += 0.15
                reasons.append("连接消息精确实体")
            if len(ends & anchors) == 2:
                score += 0.15
                reasons.append("直接连接两个锚点")
            if current in ends:
                score += 0.05
                reasons.append("连接当前用户")
            if relation.relation_type in intents:
                score += 0.15
                reasons.append("关系意图匹配")
            if (
                relation.visibility_scope == "group"
                and relation.context_id == context_id
            ):
                score += 0.05
                reasons.append("当前群可见关系")
            if atom_support:
                reasons.append("由相关记忆原子定位")
            if hop == 2:
                reasons.append("从种子实体扩展两跳")
            elif ends & anchors:
                reasons.append("从种子实体扩展一跳")
            score *= 1.0 if hop == 1 else 0.65
            scored.append(
                ScoredRelation(
                    relation=relation,
                    score=round(score * 100.0, 3),
                    reasons=list(dict.fromkeys(reasons)),
                    hop=hop,
                    supporting_memory_ids=support_ids,
                )
            )
        scored.sort(
            key=lambda item: (item.score, item.relation.updated_at), reverse=True
        )
        return scored
