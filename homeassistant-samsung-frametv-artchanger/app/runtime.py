from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class SnapshotCacheEntry:
    snapshot: Any
    fetched_at_monotonic: float


class RuntimeState:
    def __init__(self, snapshot_ttl_seconds: int = 20) -> None:
        self.snapshot_ttl_seconds = max(1, int(snapshot_ttl_seconds))

        self._lock = threading.RLock()
        self._refresh_thread: Optional[threading.Thread] = None
        self.refresh_in_progress = False
        self.last_refresh: Optional[str] = None
        self._snapshot_cache: Dict[str, SnapshotCacheEntry] = {}

    def start_refresh(self, target: Callable[[], None]) -> bool:
        with self._lock:
            if self.refresh_in_progress:
                return False

            self.refresh_in_progress = True
            thread = threading.Thread(target=self._refresh_runner, args=(target,), daemon=True, name="gallery-refresh")
            self._refresh_thread = thread

        thread.start()
        return True

    def _refresh_runner(self, target: Callable[[], None]) -> None:
        try:
            target()
        finally:
            with self._lock:
                self.refresh_in_progress = False
                self._refresh_thread = None

    def wait_for_refresh(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            thread = self._refresh_thread
        if thread:
            thread.join(timeout=timeout)

    def set_last_refresh(self, iso_timestamp: str) -> None:
        with self._lock:
            self.last_refresh = iso_timestamp

    def get_last_refresh(self) -> Optional[str]:
        with self._lock:
            return self.last_refresh

    def is_refresh_in_progress(self) -> bool:
        with self._lock:
            return self.refresh_in_progress

    def clear_snapshot_cache(self, tv_ip: Optional[str] = None) -> None:
        with self._lock:
            if tv_ip:
                self._snapshot_cache.pop(tv_ip, None)
            else:
                self._snapshot_cache.clear()

    def get_or_fetch_snapshot(
        self,
        tv_ip: str,
        fetcher: Callable[[str], Any],
        force: bool = False,
    ) -> Tuple[Any, bool]:
        if not force:
            cached = self._get_cached_snapshot(tv_ip)
            if cached is not None:
                return cached, True

        snapshot = fetcher(tv_ip)
        with self._lock:
            self._snapshot_cache[tv_ip] = SnapshotCacheEntry(snapshot=snapshot, fetched_at_monotonic=time.monotonic())
        return snapshot, False

    def _get_cached_snapshot(self, tv_ip: str) -> Optional[Any]:
        with self._lock:
            entry = self._snapshot_cache.get(tv_ip)
            if not entry:
                return None

            age = time.monotonic() - entry.fetched_at_monotonic
            if age > self.snapshot_ttl_seconds:
                self._snapshot_cache.pop(tv_ip, None)
                return None

            return entry.snapshot
