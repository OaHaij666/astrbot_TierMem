from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
import uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id(prefix: str = "mem") -> str:
    return f"{prefix}-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:6]}"


@dataclass
class MemoryEntry:
    memory_id: str
    content: str
    layer: Literal["important", "general", "fleeting"]
    category: Literal["profile", "preference", "task", "fact", "event"]
    importance: int = 3
    subject_id: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    expires_at: Optional[str] = None
    source: Literal["auto_summary", "manual", "tool_call"] = "auto_summary"
    confidence: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "layer": self.layer,
            "category": self.category,
            "importance": self.importance,
            "subject_id": self.subject_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at or "",
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        return cls(
            memory_id=data.get("memory_id", _generate_id()),
            content=data.get("content", ""),
            layer=data.get("layer", "general"),
            category=data.get("category", "fact"),
            importance=data.get("importance", 3),
            subject_id=data.get("subject_id", ""),
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            expires_at=data.get("expires_at") or None,
            source=data.get("source", "auto_summary"),
            confidence=data.get("confidence", 3),
        )


@dataclass
class MemoryState:
    important: List[MemoryEntry] = field(default_factory=list)
    general: List[MemoryEntry] = field(default_factory=list)
    fleeting: List[MemoryEntry] = field(default_factory=list)
    version: int = 1
    last_summary_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "important": [e.to_dict() for e in self.important],
            "general": [e.to_dict() for e in self.general],
            "fleeting": [e.to_dict() for e in self.fleeting],
            "version": self.version,
            "last_summary_at": self.last_summary_at or "",
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryState":
        return cls(
            important=[MemoryEntry.from_dict(e) for e in data.get("important", [])],
            general=[MemoryEntry.from_dict(e) for e in data.get("general", [])],
            fleeting=[MemoryEntry.from_dict(e) for e in data.get("fleeting", [])],
            version=data.get("version", 1),
            last_summary_at=data.get("last_summary_at") or None,
        )

    def all_entries(self) -> List[MemoryEntry]:
        return self.important + self.general + self.fleeting

    def get_layer(self, layer: str) -> List[MemoryEntry]:
        if layer == "important":
            return self.important
        elif layer == "general":
            return self.general
        elif layer == "fleeting":
            return self.fleeting
        return []


@dataclass
class ConversationTurn:
    turn_id: str
    user_message: str
    assistant_message: str
    timestamp: str
    group_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "user_message": self.user_message,
            "assistant_message": self.assistant_message,
            "timestamp": self.timestamp,
            "group_id": self.group_id or "",
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationTurn":
        return cls(
            turn_id=data.get("turn_id", _generate_id("turn")),
            user_message=data.get("user_message", ""),
            assistant_message=data.get("assistant_message", ""),
            timestamp=data.get("timestamp", _utc_now()),
            group_id=data.get("group_id") or None,
        )

    def to_prompt_text(self) -> str:
        return (
            f"[User]: {self.user_message}\n"
            f"[Assistant]: {self.assistant_message}\n"
        )


@dataclass
class SummaryOperation:
    action: Literal["add", "update", "delete", "keep"]
    layer: Optional[Literal["important", "general", "fleeting"]] = None
    memory_id: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    importance: Optional[int] = None
    reason: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SummaryOperation":
        return cls(
            action=data.get("action", "keep"),
            layer=data.get("layer"),
            memory_id=data.get("memory_id"),
            content=data.get("content"),
            category=data.get("category"),
            importance=data.get("importance"),
            reason=data.get("reason"),
        )


@dataclass
class SummaryResult:
    mode: Literal["search_replace", "full_replace"]
    summary: str = ""
    operations: List[SummaryOperation] = field(default_factory=list)
    full_state: Optional[MemoryState] = None
    cross_user_operations: Dict[str, List[SummaryOperation]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], mode: str) -> "SummaryResult":
        ops = [SummaryOperation.from_dict(o) for o in data.get("operations", [])]
        full_state = None
        if mode == "full_replace" and "full_state" in data:
            full_state = MemoryState.from_dict(data["full_state"])
        cross_ops: Dict[str, List[SummaryOperation]] = {}
        raw_cross = data.get("cross_user_operations", {})
        if isinstance(raw_cross, dict):
            for uid, op_list in raw_cross.items():
                cross_ops[uid] = [SummaryOperation.from_dict(o) for o in op_list]
        return cls(
            mode=mode,
            summary=data.get("summary", ""),
            operations=ops,
            full_state=full_state,
            cross_user_operations=cross_ops,
        )
