"""TierMem-owned orchestration for passive group observation and summarization."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

try:
    from astrbot.api import logger, sp
except ModuleNotFoundError:  # Allows the storage/orchestration tests to run standalone.
    logger = logging.getLogger("tiermem.group_observer")
    sp = None

from core.models import GroupObservation

if TYPE_CHECKING:
    from service.passive_group_capture import GroupMessageSnapshot


class GroupCapturePolicy:
    """Combine TierMem's group list with AstrBot's per-session switches."""

    plugin_names = ("TierMem", "astrbot_TierMem")

    def __init__(self, config):
        self.config = config

    async def allows(self, message: "GroupMessageSnapshot") -> bool:
        if not message.group_id or not self.config.allows_passive_group(
            message.group_id
        ):
            return False
        session_enabled, plugin_enabled = await asyncio.gather(
            self._session_enabled(message.session_id),
            self._plugin_enabled(message.session_id),
        )
        return session_enabled and plugin_enabled

    async def _session_enabled(self, session_id: str) -> bool:
        try:
            state = await sp.get_async(
                scope="umo",
                scope_id=session_id,
                key="session_service_config",
                default={},
            )
            return (
                not isinstance(state, dict) or state.get("session_enabled") is not False
            )
        except Exception as exc:
            logger.debug(f"TierMem 无法读取会话状态，沿用捕获策略: {exc}")
            return True

    async def _plugin_enabled(self, session_id: str) -> bool:
        try:
            state = await sp.get_async(
                scope="umo",
                scope_id=session_id,
                key="session_plugin_config",
                default={},
            )
            if not isinstance(state, dict):
                return True
            session_state = state.get(session_id)
            if not isinstance(session_state, dict):
                return True
            disabled = session_state.get("disabled_plugins")
            if not isinstance(disabled, list):
                return True
            return not any(name in disabled for name in self.plugin_names)
        except Exception as exc:
            logger.debug(f"TierMem 无法读取会话插件状态，沿用捕获策略: {exc}")
            return True


class GroupObserver:
    """Persist snapshots and trigger per-group summaries by count or deadline."""

    def __init__(
        self,
        config,
        repository,
        summarize: Callable[[str, str, list[GroupObservation]], Awaitable[str]],
        id_factory: Callable[[], str],
        policy=None,
    ):
        self.config = config
        self.repository = repository
        self.summarize = summarize
        self.id_factory = id_factory
        self.policy = policy or GroupCapturePolicy(config)
        self._running = False
        self._ingest_tasks = set()
        self._summary_tasks = {}
        self._group_locks = {}
        self._failures = {}
        self._deadlines = {}
        self._deadline_changed = asyncio.Event()
        self._scheduler_task = None

    async def start(self):
        if self._running:
            return
        self._running = True
        await self.refresh_deadlines()
        self._scheduler_task = asyncio.create_task(self._deadline_scheduler())

    async def stop(self):
        self._running = False
        self._deadline_changed.set()
        tasks = [
            *self._ingest_tasks,
            *self._summary_tasks.values(),
            *([self._scheduler_task] if self._scheduler_task else []),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ingest_tasks.clear()
        self._summary_tasks.clear()
        self._deadlines.clear()
        self._failures.clear()
        self._scheduler_task = None

    def submit(self, message: "GroupMessageSnapshot") -> None:
        """Accept a snapshot without blocking AstrBot's synchronous filter."""
        if not self._running or not self.config.enable_passive_group_capture:
            return
        task = asyncio.create_task(self._ingest(message))
        self._ingest_tasks.add(task)
        task.add_done_callback(self._ingest_tasks.discard)

    async def reconfigure(self):
        """Rebuild deadlines after a live settings update."""
        await self.refresh_deadlines()

    async def refresh_deadlines(self):
        self._deadlines.clear()
        if (
            self._running
            and self.config.enable_passive_group_capture
            and self.config.passive_group_max_wait_minutes > 0
        ):
            for stream in await self.repository.list_streams():
                context_id = stream["context_id"]
                if stream["message_count"] >= self.config.passive_group_fifo_size:
                    self._schedule_summary(context_id, stream["group_id"])
                else:
                    self._arm_deadline(
                        context_id,
                        stream["group_id"],
                        self._parse_time(stream["oldest_at"]),
                    )
        self._deadline_changed.set()

    async def _ingest(self, message: "GroupMessageSnapshot"):
        try:
            if not await self.policy.allows(message):
                return
            if message.self_user_id and message.sender_user_id == message.self_user_id:
                return
            content = message.content.strip()
            if len(
                content
            ) < self.config.passive_group_min_message_length or content.startswith("/"):
                return
            observation = GroupObservation(
                observation_id=(
                    f"{message.context_id}:{message.message_id}"
                    if message.message_id
                    else self.id_factory()
                ),
                context_id=message.context_id,
                group_id=message.group_id,
                sender_user_id=message.sender_user_id or "unknown",
                sender_name=message.sender_name or message.sender_user_id or "unknown",
                content=content[:4000],
            )
            if not await self.repository.append(observation):
                return
            await self.repository.trim(
                message.context_id, self.config.passive_group_max_buffer
            )
            count = await self.repository.count(message.context_id)
            if count >= self.config.passive_group_fifo_size:
                self._schedule_summary(message.context_id, message.group_id)
            elif message.context_id not in self._deadlines:
                self._arm_deadline(
                    message.context_id,
                    message.group_id,
                    self._parse_time(observation.timestamp),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"TierMem 群观察写入失败: {exc}")

    def _schedule_summary(self, context_id: str, group_id: str) -> bool:
        if not self._running or not self.config.enable_passive_group_capture:
            return False
        existing = self._summary_tasks.get(context_id)
        if existing and not existing.done():
            return False
        failure = self._failures.get(context_id)
        now = datetime.now(timezone.utc)
        if failure and now < failure["retry_after"]:
            self._deadlines[context_id] = (failure["retry_after"], group_id)
            self._deadline_changed.set()
            return False

        async def runner():
            completed = False
            try:
                completed = await self._run_summary(context_id, group_id)
            finally:
                self._summary_tasks.pop(context_id, None)
            if completed:
                await self._rearm_from_store(context_id)

        self._deadlines.pop(context_id, None)
        self._summary_tasks[context_id] = asyncio.create_task(runner())
        return True

    async def _run_summary(self, context_id: str, group_id: str):
        async with self._group_lock(context_id):
            observations = await self.repository.get(
                context_id, self.config.passive_group_max_buffer
            )
            if not observations:
                return True
            try:
                summary = await self.summarize(context_id, group_id, observations)
                self._failures.pop(context_id, None)
                logger.info(f"群 {group_id} 观察总结完成: {summary[:80]}")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"群 {group_id} 观察总结异常: {exc}")
                self._record_failure(context_id, group_id)
                return False

    def _record_failure(self, context_id: str, group_id: str):
        attempts = self._failures.get(context_id, {}).get("attempts", 0) + 1
        delay = min(60, 5 * (2 ** min(attempts - 1, 4)))
        retry_after = datetime.now(timezone.utc) + timedelta(minutes=delay)
        self._failures[context_id] = {
            "attempts": attempts,
            "retry_after": retry_after,
        }
        self._deadlines[context_id] = (retry_after, group_id)
        self._deadline_changed.set()

    async def _rearm_from_store(self, context_id: str):
        stream = await self.repository.get_stream(context_id)
        if stream is None:
            self._deadlines.pop(context_id, None)
            return
        if stream["message_count"] >= self.config.passive_group_fifo_size:
            self._schedule_summary(context_id, stream["group_id"])
        else:
            self._arm_deadline(
                context_id,
                stream["group_id"],
                self._parse_time(stream["oldest_at"]),
            )

    def _arm_deadline(self, context_id: str, group_id: str, oldest_at: datetime):
        minutes = float(self.config.passive_group_max_wait_minutes)
        if minutes <= 0:
            return
        deadline = oldest_at + timedelta(minutes=minutes)
        current = self._deadlines.get(context_id)
        if current is None or deadline < current[0]:
            self._deadlines[context_id] = (deadline, group_id)
            self._deadline_changed.set()

    async def _deadline_scheduler(self):
        try:
            while self._running:
                self._deadline_changed.clear()
                if (
                    not self.config.enable_passive_group_capture
                    or self.config.passive_group_max_wait_minutes <= 0
                    or not self._deadlines
                ):
                    await self._deadline_changed.wait()
                    continue
                context_id, (deadline, group_id) = min(
                    self._deadlines.items(), key=lambda item: item[1][0]
                )
                delay = (deadline - datetime.now(timezone.utc)).total_seconds()
                if delay <= 0:
                    self._deadlines.pop(context_id, None)
                    self._schedule_summary(context_id, group_id)
                    continue
                try:
                    await asyncio.wait_for(self._deadline_changed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    def _group_lock(self, context_id: str):
        if context_id not in self._group_locks:
            self._group_locks[context_id] = asyncio.Lock()
        return self._group_locks[context_id]

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
