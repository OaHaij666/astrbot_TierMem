"""Capture ordinary group messages without waking AstrBot's reply pipeline."""

import weakref

from astrbot.api import logger, sp
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType


_ACTIVE_PLUGIN_REF = None
_PLUGIN_NAMES = ("TierMem", "astrbot_TierMem")


def _plugin_ref(plugin):
    """Prefer a weak reference, but support non-weak-referenceable host proxies."""
    if plugin is None:
        return None
    try:
        return weakref.ref(plugin)
    except TypeError:
        return lambda: plugin


def set_active_plugin(plugin) -> None:
    global _ACTIVE_PLUGIN_REF
    _ACTIVE_PLUGIN_REF = _plugin_ref(plugin)


def get_active_plugin():
    return _ACTIVE_PLUGIN_REF() if _ACTIVE_PLUGIN_REF is not None else None


async def session_allows_capture(session_id: str) -> bool:
    """Honor AstrBot's per-session and per-plugin disable switches."""
    try:
        services = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_service_config",
            default={},
        )
        if isinstance(services, dict) and services.get("session_enabled") is False:
            return False
        plugin_config = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_plugin_config",
            default={},
        )
        if not isinstance(plugin_config, dict):
            return True
        session_config = plugin_config.get(session_id, {})
        disabled = (
            session_config.get("disabled_plugins", [])
            if isinstance(session_config, dict)
            else []
        )
        return not any(name in disabled for name in _PLUGIN_NAMES)
    except Exception as exc:
        logger.debug(f"TierMem 读取会话开关失败，默认允许被动捕获: {exc}")
        return True


class PassiveGroupCaptureFilter(CustomFilter):
    """Schedule capture as a side effect, then return False to avoid waking Bot."""

    def __init__(self, raise_error: bool = True, plugin=None, **kwargs):
        if not isinstance(raise_error, bool) and plugin is None:
            plugin = raise_error
            raise_error = True
        super().__init__(raise_error=raise_error, **kwargs)
        self._plugin_ref = _plugin_ref(plugin)

    def _plugin(self):
        return (
            self._plugin_ref() if self._plugin_ref is not None else get_active_plugin()
        )

    def filter(self, event, _cfg) -> bool:
        plugin = self._plugin()
        if not plugin or not getattr(plugin, "_initialized", False):
            return False
        config = getattr(plugin, "config", None)
        if not config or not config.enable_passive_group_capture:
            return False
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return False
            group_id = str(event.get_group_id() or "").strip()
        except Exception as exc:
            logger.debug(f"TierMem 被动群消息类型检查失败: {exc}")
            return False
        if group_id and config.allows_passive_group(group_id):
            plugin._schedule_passive_group_capture(event)
        return False
