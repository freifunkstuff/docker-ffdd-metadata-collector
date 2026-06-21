from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime, timedelta, timezone

from .config import MetadataCollectorConfig
from .fetcher import SysinfoFetcher
from .meshviewer import build_community_meshviewer_documents, build_meshviewer_document
from .models import NodeState
from .node_list_sources import BmxdNodeListSource, FileJsonNodeListSource, HttpJsonNodeListSource, NodeListSource, NodeListSourceError
from .scheduler import PollScheduler, classify_poll_mode, compute_next_poll_at, fetch_timeout_for_mode
from .snapshot import build_fetch_summary, build_snapshot_document, build_status_document, write_published_json_atomic
from .storage import StateStore, YamlBackedMemoryStore


logger = logging.getLogger(__name__)


class MetadataCollectorApp:
    def __init__(self, config: MetadataCollectorConfig) -> None:
        self.config = config
        self.source = self._create_source()
        self.fetcher = SysinfoFetcher(user_agent=config.request_user_agent)
        self.store = self._create_store()
        self.scheduler = PollScheduler()
        self._scheduler_wakeup = asyncio.Event()
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        self.config.ensure_directories()
        self.store.initialize()
        self._prune_retained_nodes(reason="startup")
        scheduled_count = self._bootstrap_scheduler()
        self._install_signal_handlers()
        logger.info(
            "collector starting source_type=%s source=%s state_dir=%s node_metadata_path=%s status_path=%s concurrency=%s timeouts=%s/%s/%s scheduled=%s",
            self.config.source_type,
            self._source_label(),
            self.config.state_dir,
            self.config.node_metadata_path,
            self.config.status_path,
            self.config.fetch_concurrency,
            self.config.fetch_timeout_normal_seconds,
            self.config.fetch_timeout_slow_seconds,
            self.config.fetch_timeout_very_slow_seconds,
            scheduled_count,
        )
        logger.info("parallel fetches=%s", self.config.fetch_concurrency)
        self._log_loaded_persistence_summary()
        await self._write_outputs(reason="startup-persistence")
        await self._run_discovery_once(initial=True)
        await self._write_outputs(reason="startup-discovery")

        tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery-loop"),
            asyncio.create_task(self._poll_loop(), name="poll-loop"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot-loop"),
            asyncio.create_task(self._summary_loop(), name="summary-loop"),
        ]
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._remove_signal_handlers()
            self._log_summary()
            logger.info("collector stopped")

    def stop(self) -> None:
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._request_shutdown, sig)

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)

    def _request_shutdown(self, sig: signal.Signals) -> None:
        logger.info("shutdown requested signal=%s", sig.name)
        self.stop()

    def _create_source(self) -> NodeListSource:
        if self.config.source_type == "http-json":
            return HttpJsonNodeListSource(
                url=self.config.source_url,
                timeout_seconds=self.config.fetch_timeout_normal_seconds,
                user_agent=self.config.request_user_agent,
            )
        if self.config.source_type == "file-json":
            return FileJsonNodeListSource(path=self.config.source_path)
        if self.config.source_type == "bmxd":
            return BmxdNodeListSource()
        raise ValueError(f"unsupported METADATA_COLLECTOR_SOURCE: {self.config.source_type}")

    def _create_store(self) -> StateStore:
        if self.config.storage_backend == "yaml-memory":
            return YamlBackedMemoryStore(
                discovery_state_path=self.config.discovery_state_path,
                node_info_dir=self.config.node_info_dir,
                node_status_dir=self.config.node_status_dir,
            )
        raise ValueError(f"unsupported METADATA_COLLECTOR_STORAGE: {self.config.storage_backend}")

    def _bootstrap_scheduler(self) -> int:
        now = _utcnow()
        scheduled_count = 0
        for state in self.store.list_node_states():
            due_at = _parse_or_now(state.next_poll_at, now)
            self.scheduler.schedule(state.node_id, due_at)
            scheduled_count += 1
        return scheduled_count

    def _schedule_node(self, node_id: str, due_at: datetime) -> None:
        self.scheduler.schedule(node_id, due_at)
        self._scheduler_wakeup.set()

    async def _run_discovery_once(self, initial: bool = False) -> None:
        discovered_at = _utcnow().isoformat()
        try:
            nodes = await self.source.fetch_nodes()
        except NodeListSourceError as exc:
            logger.warning("discovery failed source=%s error=%s", self._source_label(), exc)
            return
        known_node_ids = {state.node_id for state in self.store.list_node_states()}
        self.store.merge_discovered_nodes(nodes, discovered_at)
        self._prune_retained_nodes(reason="discovery")
        new_node_count = 0
        for node in nodes:
            if node.node_id not in known_node_ids:
                self._schedule_node(node.node_id, _utcnow())
                new_node_count += 1
        if initial or new_node_count > 0:
            logger.info(
                "discovery completed total=%s new=%s existing=%s",
                len(nodes),
                new_node_count,
                len(nodes) - new_node_count,
            )
        else:
            logger.debug("discovery completed total=%s without changes", len(nodes))

    def _source_label(self) -> str:
        if self.config.source_type == "file-json":
            return str(self.config.source_path)
        return self.config.source_url

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.discovery_interval_seconds)
            await self._run_discovery_once()

    async def _poll_loop(self) -> None:
        semaphore = asyncio.Semaphore(self.config.fetch_concurrency)
        while True:
            now = _utcnow()
            due_node_ids = self.scheduler.pop_due(now, self.config.fetch_concurrency)
            if not due_node_ids:
                timeout = self.scheduler.seconds_until_next_due(now)
                try:
                    await asyncio.wait_for(self._scheduler_wakeup.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._scheduler_wakeup.clear()
                continue
            await asyncio.gather(*(self._poll_node(node_id, semaphore) for node_id in due_node_ids))

    async def _poll_node(self, node_id: str, semaphore: asyncio.Semaphore) -> None:
        state = self.store.get_node_state(node_id)
        if state is None:
            return
        try:
            poll_mode = classify_poll_mode(self.config, state, _utcnow())
            timeout_seconds = fetch_timeout_for_mode(self.config, poll_mode)
            async with semaphore:
                outcome = await self.fetcher.fetch(node_id=node_id, primary_ip=state.primary_ip, timeout_seconds=timeout_seconds)
            now = _utcnow()
            self.store.apply_fetch_outcome(outcome)
            refreshed_state = self.store.get_node_state(node_id) or state
            next_poll_at = compute_next_poll_at(self.config, refreshed_state, outcome, now)
            refreshed_state.next_poll_at = next_poll_at.isoformat()
            self._schedule_node(node_id, next_poll_at)
        except Exception:
            retry_at = _utcnow() + timedelta(seconds=self.config.poll_interval_slow_seconds)
            logger.exception("poll failed node_id=%s primary_ip=%s retry_at=%s", node_id, state.primary_ip, retry_at.isoformat())
            self._schedule_node(node_id, retry_at)

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.snapshot_interval_seconds)
            await self._write_outputs()

    async def _summary_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.log_summary_interval_seconds)
            self._log_summary()

    async def _write_outputs(self, reason: str | None = None) -> None:
        generated_at = _utcnow().isoformat()
        states = self.store.list_node_states()
        node_metadata_document = build_snapshot_document(generated_at, states)
        status_document = build_status_document(
            generated_at=generated_at,
            states=states,
            online_window_seconds=self.config.online_window_seconds,
            fetch_window_seconds=self.config.log_summary_interval_seconds,
            source_type=self.config.source_type,
            source=self._source_label(),
        )
        meshviewer_document = build_meshviewer_document(
            generated_at=generated_at,
            states=states,
            online_window_seconds=self.config.meshviewer_online_window_seconds,
            hide_temp_after_seconds=self.config.meshviewer_hide_temp_after_seconds,
            hide_stale_after_days=self.config.meshviewer_hide_stale_after_days,
        )
        community_meshviewer_documents = build_community_meshviewer_documents(
            generated_at=generated_at,
            states=states,
            online_window_seconds=self.config.meshviewer_online_window_seconds,
            hide_temp_after_seconds=self.config.meshviewer_hide_temp_after_seconds,
            hide_stale_after_days=self.config.meshviewer_hide_stale_after_days,
        )
        publish_tasks = [
            asyncio.to_thread(
                write_published_json_atomic,
                self.config.node_metadata_path,
                self.config.published_node_metadata_path,
                node_metadata_document,
            ),
            asyncio.to_thread(
                write_published_json_atomic,
                self.config.status_path,
                self.config.published_status_path,
                status_document,
            ),
            asyncio.to_thread(
                write_published_json_atomic,
                self.config.meshviewer_path,
                self.config.published_meshviewer_path,
                meshviewer_document,
            ),
        ]
        for community_slug, community_document in community_meshviewer_documents.items():
            publish_tasks.append(
                asyncio.to_thread(
                    write_published_json_atomic,
                    self.config.meshviewer_path.parent / community_slug / self.config.meshviewer_path.name,
                    self.config.published_meshviewer_path.parent / community_slug / self.config.published_meshviewer_path.name,
                    community_document,
                )
            )
        await asyncio.gather(*publish_tasks)
        if reason is not None:
            logger.info(
                "outputs written reason=%s nodes=%s node_metadata_path=%s status_path=%s meshviewer_path=%s meshviewer_communities=%s",
                reason,
                len(node_metadata_document["nodes"]),
                self.config.node_metadata_path,
                self.config.status_path,
                self.config.meshviewer_path,
                len(community_meshviewer_documents),
            )
        else:
            logger.debug(
                "outputs written nodes=%s node_metadata_path=%s status_path=%s meshviewer_path=%s meshviewer_communities=%s",
                len(node_metadata_document["nodes"]),
                self.config.node_metadata_path,
                self.config.status_path,
                self.config.meshviewer_path,
                len(community_meshviewer_documents),
            )

    def _log_summary(self) -> None:
        now = _utcnow()
        states = self.store.list_node_states()
        summary = self._summarize_states(states, now)
        fetch_summary = build_fetch_summary(now, states, self.config.log_summary_interval_seconds)
        logger.info(
            "summary nodes=%s online=%s stale=%s unknown=%s fetches=%s rate_per_minute=%s window_seconds=%s",
            summary["total"],
            summary["online"],
            summary["stale"],
            summary["unknown"],
            fetch_summary["fetches"],
            fetch_summary["ratePerMinute"],
            fetch_summary["windowSeconds"],
        )

    def _log_loaded_persistence_summary(self) -> None:
        now = _utcnow()
        summary = self._summarize_states(self.store.list_node_states(), now)
        logger.info(
            "persistence loaded nodes=%s online=%s stale=%s unknown=%s with_source_seen=%s with_success=%s freshest_age_seconds=%s stalest_online_age_seconds=%s",
            summary["total"],
            summary["online"],
            summary["stale"],
            summary["unknown"],
            summary["with_source_seen"],
            summary["with_success"],
            summary["freshest_age_seconds"],
            summary["stalest_online_age_seconds"],
        )

    def _prune_retained_nodes(self, reason: str) -> None:
        if not isinstance(self.store, YamlBackedMemoryStore):
            return
        removed = self.store.purge_nodes_older_than(_utcnow(), self.config.node_retention_seconds)
        if removed > 0:
            logger.info("retention pruned nodes=%s reason=%s retention_seconds=%s", removed, reason, self.config.node_retention_seconds)

    def _summarize_states(self, states: list[NodeState], now: datetime) -> dict[str, int | float | None]:
        online = 0
        stale = 0
        unknown = 0
        with_source_seen = 0
        with_success = 0
        freshest_age_seconds: float | None = None
        stalest_online_age_seconds: float | None = None

        for state in states:
            if state.last_source_seen_at:
                with_source_seen += 1
            if state.last_success_at:
                with_success += 1

            is_online = state.is_online(now, self.config.online_window_seconds)
            if is_online is True:
                online += 1
            elif is_online is False:
                stale += 1
            else:
                unknown += 1

            last_seen = state.last_seen_for_snapshot()
            if last_seen is None:
                continue
            age_seconds = (now - _parse_or_now(last_seen, now)).total_seconds()
            if freshest_age_seconds is None or age_seconds < freshest_age_seconds:
                freshest_age_seconds = age_seconds
            if is_online is True and (stalest_online_age_seconds is None or age_seconds > stalest_online_age_seconds):
                stalest_online_age_seconds = age_seconds

        return {
            "total": len(states),
            "online": online,
            "stale": stale,
            "unknown": unknown,
            "with_source_seen": with_source_seen,
            "with_success": with_success,
            "freshest_age_seconds": None if freshest_age_seconds is None else round(freshest_age_seconds, 3),
            "stalest_online_age_seconds": None if stalest_online_age_seconds is None else round(stalest_online_age_seconds, 3),
        }

async def run_from_env(config: MetadataCollectorConfig | None = None) -> None:
    app = MetadataCollectorApp(config or MetadataCollectorConfig.from_env())
    await app.run()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_or_now(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
