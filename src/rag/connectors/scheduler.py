"""
Background scheduler for the Confluence connector.

Runs sync per space every cfg.sync_interval_hours. Survives restarts
by reading per-space last_run from Postgres and only syncing spaces
whose interval has elapsed. Same lifecycle pattern as
ingest/watcher.IngestWatcher: start() in FastAPI lifespan, stop() on
shutdown.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .confluence import (
    load_config, sync_space, last_sync, ensure_schema,
    ConfluenceClient, ConfluenceConfig, SpaceConfig,
)

log = logging.getLogger(__name__)


class ConfluenceScheduler:
    def __init__(self, cfg: ConfluenceConfig | None = None):
        self.cfg = cfg or load_config()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg is not None and bool(self.cfg.spaces)

    def start(self) -> "ConfluenceScheduler":
        if not self.enabled:
            log.info("confluence scheduler: no config/spaces — disabled")
            return self
        ensure_schema()
        self._thread = threading.Thread(
            target=self._run, name="confluence-scheduler", daemon=True,
        )
        self._thread.start()
        log.info(
            "confluence scheduler: started (interval=%.1fh, spaces=%s)",
            self.cfg.sync_interval_hours,
            [s.key for s in self.cfg.spaces],
        )
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _due_spaces(self) -> list[SpaceConfig]:
        now = datetime.now(timezone.utc)
        cutoff = self.cfg.sync_interval_hours * 3600
        due = []
        for sp in self.cfg.spaces:
            row = last_sync(sp.key)
            lr = row["last_run"] if row and row.get("last_run") else None
            if lr is None or (now - lr).total_seconds() >= cutoff:
                due.append(sp)
        return due

    def _next_sleep(self) -> float:
        """Seconds until the soonest space is due; clamped to [60, 3600]."""
        now = datetime.now(timezone.utc)
        cutoff = self.cfg.sync_interval_hours * 3600
        waits: list[float] = []
        for sp in self.cfg.spaces:
            row = last_sync(sp.key)
            lr = row["last_run"] if row and row.get("last_run") else None
            if lr is None:
                return 60.0
            waits.append(max(0.0, cutoff - (now - lr).total_seconds()))
        soonest = min(waits, default=cutoff)
        return max(60.0, min(soonest, 3600.0))

    def _run(self) -> None:
        self._stop.wait(10)  # let infra come up
        while not self._stop.is_set():
            try:
                due = self._due_spaces()
                if due:
                    log.info("confluence scheduler: %d space(s) due: %s",
                             len(due), [s.key for s in due])
                    client = ConfluenceClient(self.cfg)
                    try:
                        for sp in due:
                            if self._stop.is_set():
                                break
                            r = sync_space(client, sp)
                            log.info(
                                "confluence[%s] sync done: seen=%d changed=%d "
                                "deleted=%d chunks=%d in %.1fs%s",
                                r.space, r.seen, r.changed, r.deleted,
                                r.chunks, r.duration_s,
                                f" ERROR={r.error}" if r.error else "",
                            )
                    finally:
                        client.close()
            except Exception:
                log.exception("confluence scheduler tick failed")
            self._stop.wait(self._next_sleep())


def start_if_configured() -> ConfluenceScheduler | None:
    import os
    if os.environ.get("CONFLUENCE_ENABLED", "true").lower() != "true":
        log.info("confluence scheduler: CONFLUENCE_ENABLED=false")
        return None
    sched = ConfluenceScheduler()
    if not sched.enabled:
        return None
    return sched.start()
