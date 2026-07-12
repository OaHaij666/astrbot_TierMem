"""Minimal AstrBot adapter for observing group messages without handling them."""

from dataclasses import dataclass
from typing import Callable, Optional

from astrbot.api import logger
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType


@dataclass(frozen=True, slots=True)
class GroupMessageSnapshot:
    """Immutable fields copied from an AstrBot event before it leaves the hook."""

    session_id: str
    group_id: str
    sender_user_id: str
    sender_name: str
    self_user_id: str
    content: str
    message_id: str

    @property
    def context_id(self) -> str:
        return f"group:{self.group_id}"


CaptureSink = Callable[[GroupMessageSnapshot], None]
_capture_target: Optional[tuple[object, CaptureSink]] = None


def bind_capture_sink(sink: CaptureSink) -> object:
    """Publish the current plugin's non-blocking snapshot receiver."""
    global _capture_target
    token = object()
    _capture_target = (token, sink)
    return token


def unbind_capture_sink(token: object) -> None:
    """Remove a receiver only when the caller still owns the binding."""
    global _capture_target
    if _capture_target is not None and _capture_target[0] is token:
        _capture_target = None


def _call_text(event, method_name: str, fallback: str = "") -> str:
    method = getattr(event, method_name, None)
    if callable(method):
        try:
            value = method()
            if value is not None:
                return str(value)
        except Exception:
            pass
    return str(fallback or "")


def snapshot_group_message(event) -> GroupMessageSnapshot:
    """Copy only the data required by TierMem's background observer."""
    sender_user_id = _call_text(event, "get_sender_id")
    sender_name = _call_text(event, "get_sender_name", sender_user_id)
    message_obj = getattr(event, "message_obj", None)
    return GroupMessageSnapshot(
        session_id=str(getattr(event, "unified_msg_origin", "") or ""),
        group_id=_call_text(event, "get_group_id"),
        sender_user_id=sender_user_id,
        sender_name=sender_name or sender_user_id,
        self_user_id=_call_text(event, "get_self_id"),
        content=str(getattr(event, "message_str", "") or ""),
        message_id=str(getattr(message_obj, "message_id", "") or ""),
    )


class PassiveGroupMessageTap(CustomFilter):
    """Copy a group event to the registered sink, then decline the handler."""

    def filter(self, event, _config) -> bool:
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return False
            target = _capture_target
            if target is not None:
                target[1](snapshot_group_message(event))
        except Exception as exc:
            logger.debug(f"TierMem 群消息快照创建失败: {exc}")
        return False
