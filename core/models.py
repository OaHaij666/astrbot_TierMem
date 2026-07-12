from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import exp, log
from typing import Any, Dict, List, Literal, Optional


MemoryLayer = Literal["core", "semantic", "episodic", "working"]
MemoryCategory = Literal["profile", "preference", "task", "fact", "event", "relation"]
EntityType = Literal["user", "group", "project", "organization", "topic", "other"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decay_rate_from_half_life(half_life_days: float) -> float:
    if half_life_days <= 0:
        return 0.0
    return log(2.0) / half_life_days


def decayed_strength(
    strength: float,
    decay_rate: float,
    last_confirmed_at: str,
    stability: float = 0.5,
    now: Optional[datetime] = None,
) -> float:
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - parse_time(last_confirmed_at)).total_seconds() / 86400.0)
    # 稳定性越高，实际衰减越慢；0.5 保持配置给出的标准速率。
    stability_factor = max(0.1, 1.5 - max(0.0, min(stability, 1.0)))
    return max(0.0, min(1.0, strength * exp(-decay_rate * stability_factor * age_days)))


@dataclass
class MemoryEntry:
    memory_id: str
    owner_user_id: str
    content: str
    layer: MemoryLayer = "semantic"
    category: MemoryCategory = "fact"
    importance: int = 3
    confidence: float = 0.7
    strength: float = 0.7
    stability: float = 0.5
    decay_rate: float = 0.0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_accessed_at: Optional[str] = None
    last_confirmed_at: str = field(default_factory=utc_now)
    confirmation_count: int = 1
    expires_at: Optional[str] = None
    source: str = "auto_summary"
    source_turn_id: Optional[str] = None
    visibility_scope: str = "private"
    context_id: Optional[str] = None
    status: str = "active"

    # Compatibility alias for older call sites while the plugin API remains stable.
    @property
    def subject_id(self) -> str:
        return self.owner_user_id

    @subject_id.setter
    def subject_id(self, value: str) -> None:
        self.owner_user_id = value

    def effective_strength(self, now: Optional[datetime] = None) -> float:
        if self.expires_at and parse_time(self.expires_at) <= (
            now or datetime.now(timezone.utc)
        ):
            return 0.0
        return decayed_strength(
            self.strength, self.decay_rate, self.last_confirmed_at, self.stability, now
        )

    def retrieval_score(self, now: Optional[datetime] = None) -> float:
        return self.effective_strength(now) * (self.importance / 5.0) * self.confidence

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        allowed = cls.__dataclass_fields__.keys()
        values = {k: v for k, v in data.items() if k in allowed}
        values.setdefault("memory_id", "")
        values.setdefault("owner_user_id", data.get("subject_id", ""))
        values.setdefault("content", "")
        return cls(**values)


@dataclass
class MemoryState:
    core: List[MemoryEntry] = field(default_factory=list)
    semantic: List[MemoryEntry] = field(default_factory=list)
    episodic: List[MemoryEntry] = field(default_factory=list)
    working: List[MemoryEntry] = field(default_factory=list)

    # Compatibility views used by a few external integrations.
    @property
    def important(self) -> List[MemoryEntry]:
        return self.core

    @property
    def general(self) -> List[MemoryEntry]:
        return self.semantic

    @property
    def fleeting(self) -> List[MemoryEntry]:
        return self.working

    def all_entries(self) -> List[MemoryEntry]:
        return self.core + self.semantic + self.episodic + self.working

    def get_layer(self, layer: str) -> List[MemoryEntry]:
        return list(getattr(self, layer, []))

    def to_dict(self) -> Dict[str, Any]:
        return {
            layer: [e.to_dict() for e in self.get_layer(layer)]
            for layer in ("core", "semantic", "episodic", "working")
        }


@dataclass
class Entity:
    entity_id: str
    entity_type: EntityType
    name: str
    aliases: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass
class Relation:
    relation_id: str
    source_entity_id: str
    relation_type: str
    target_entity_id: str
    confidence: float = 0.7
    strength: float = 0.7
    stability: float = 0.5
    decay_rate: float = 0.0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_confirmed_at: str = field(default_factory=utc_now)
    confirmation_count: int = 1
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    status: str = "active"
    visibility_scope: str = "private"
    context_id: Optional[str] = None
    owner_user_id: str = ""

    def effective_strength(self, now: Optional[datetime] = None) -> float:
        if self.valid_until and parse_time(self.valid_until) <= (
            now or datetime.now(timezone.utc)
        ):
            return 0.0
        return decayed_strength(
            self.strength, self.decay_rate, self.last_confirmed_at, self.stability, now
        )


@dataclass
class RelationEvidence:
    evidence_id: str
    relation_id: str
    excerpt: str
    speaker_user_id: str = ""
    turn_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    memory_id: Optional[str] = None
    polarity: str = "support"
    evidence_weight: float = 1.0


@dataclass
class ConversationTurn:
    turn_id: str
    user_id: str
    user_message: str
    assistant_message: str
    timestamp: str
    context_id: Optional[str] = None
    group_id: Optional[str] = None

    def to_prompt_text(self) -> str:
        return (
            f"[Context {self.context_id or 'unknown'}] [User {self.user_id}]: {self.user_message}\n"
            f"[Assistant]: {self.assistant_message}\n"
        )


@dataclass
class MemoryOperation:
    action: str
    memory_id: Optional[str] = None
    content: Optional[str] = None
    layer: Optional[str] = None
    category: Optional[str] = None
    importance: Optional[int] = None
    confidence: Optional[float] = None
    stability: Optional[float] = None
    visibility_scope: Optional[str] = None
    entity_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryOperation":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class RelationOperation:
    action: str
    relation_id: Optional[str] = None
    source_entity_id: Optional[str] = None
    source_entity_type: str = "user"
    source_name: Optional[str] = None
    source_aliases: List[str] = field(default_factory=list)
    relation_type: Optional[str] = None
    target_entity_id: Optional[str] = None
    target_entity_type: str = "user"
    target_name: Optional[str] = None
    target_aliases: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    stability: Optional[float] = None
    visibility_scope: Optional[str] = None
    evidence: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RelationOperation":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SummaryResult:
    summary: str = ""
    memory_operations: List[MemoryOperation] = field(default_factory=list)
    relation_operations: List[RelationOperation] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SummaryResult":
        return cls(
            summary=data.get("summary", ""),
            memory_operations=[
                MemoryOperation.from_dict(x) for x in data.get("memory_operations", [])
            ],
            relation_operations=[
                RelationOperation.from_dict(x)
                for x in data.get("relation_operations", [])
            ],
        )
