from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import (
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from hooks import load_hooks
from hooks.sync_hook import BaseHook, ChangeEvent

logger = logging.getLogger(__name__)


class DebouncedHandler(FileSystemEventHandler):
    """
    Watchdog event handler with per-path debouncing.

    Design:
    - For each file path, maintain a pending threading.Timer.
    - Each new event for the same path cancels and replaces the timer.
    - After debounce_seconds of silence on a path, the timer fires and
      dispatches a ChangeEvent to the hook chain.

    Circular sync prevention:
    - Before scheduling a timer, check if the path is in ignore_paths.
    - ignore_paths is owned by DriveSync and populated before it writes
      downloaded files to disk.
    """

    # 동기화 제외 폴더 (옵시디언 내부 + 플러그인 자동 생성 파일 — API 호출 폭주 방지)
    IGNORE_DIRS = {".obsidian", ".smart-env", ".trash"}

    def __init__(
        self,
        hooks: list[BaseHook],
        debounce_seconds: float,
        ignore_paths: set[str],
        ignore_lock: threading.Lock,
    ):
        super().__init__()
        self._hooks = hooks
        self._debounce_seconds = debounce_seconds
        self._ignore_paths = ignore_paths   # DriveSync와 공유 (순환 방지용)
        self._ignore_lock = ignore_lock
        self._timers: dict[str, threading.Timer] = {}
        self._timers_lock = threading.Lock()

    def _is_ignored(self, path: str) -> bool:
        parts = path.replace("\\", "/").split("/")
        if any(p in self.IGNORE_DIRS for p in parts):
            return True
        with self._ignore_lock:
            return path in self._ignore_paths

    def _schedule(
        self,
        event_type: str,
        src_path: str,
        dest_path: str | None,
        is_directory: bool,
    ) -> None:
        """Cancel existing timer for path and schedule a fresh one."""
        if self._is_ignored(src_path):
            logger.debug("Ignoring circular event for %s", src_path)
            return

        with self._timers_lock:
            existing = self._timers.get(src_path)
            if existing:
                existing.cancel()

            timer = threading.Timer(
                self._debounce_seconds,
                self._dispatch,
                args=(event_type, src_path, dest_path, is_directory),
            )
            self._timers[src_path] = timer
            timer.daemon = True
            timer.start()

    def _dispatch(
        self,
        event_type: str,
        src_path: str,
        dest_path: str | None,
        is_directory: bool,
    ) -> None:
        """Called by the timer after debounce_seconds. Runs hooks sequentially."""
        with self._timers_lock:
            self._timers.pop(src_path, None)

        event = ChangeEvent(
            event_type=event_type,
            src_path=Path(src_path),
            dest_path=Path(dest_path) if dest_path else None,
            is_directory=is_directory,
        )

        for hook in self._hooks:
            try:
                hook.on_change(event)
            except Exception:
                logger.exception(
                    "Hook %s raised on %s", type(hook).__name__, src_path
                )

    # ── watchdog callbacks ────────────────────────────────────────────────

    def on_created(self, event):
        self._schedule("created", event.src_path, None, event.is_directory)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule("modified", event.src_path, None, False)

    def on_deleted(self, event):
        self._schedule("deleted", event.src_path, None, event.is_directory)

    def on_moved(self, event):
        self._schedule(
            "moved", event.src_path, event.dest_path, event.is_directory
        )


class VaultWatcher:
    """
    Manages one watchdog Observer that covers all configured watch_paths.
    Exposes start() / stop() for the daemon lifecycle.
    """

    def __init__(self, config: dict, drive_sync):
        self._config = config
        self._drive_sync = drive_sync
        self._observer = Observer()
        self._watched: list[str] = []
        self._all_hooks: list[BaseHook] = []  # for on_shutdown() calls

    def setup(self) -> None:
        """
        Wire up a DebouncedHandler for each configured watch_path.
        Must be called before start().
        """
        # Default fallback prevents KeyError on missing/typo'd config key
        debounce = self._config.get("sync", {}).get("debounce_seconds", 5)
        ignore_paths = self._drive_sync.ignore_paths
        ignore_lock = self._drive_sync.ignore_lock

        for entry in self._config["watch_paths"]:
            path = str(Path(entry["path"]).expanduser().resolve())
            hook_names: list[str] = entry["hooks"]

            hooks = load_hooks(hook_names, drive_sync=self._drive_sync)
            self._all_hooks.extend(hooks)  # track for on_shutdown()

            handler = DebouncedHandler(
                hooks=hooks,
                debounce_seconds=debounce,
                ignore_paths=ignore_paths,
                ignore_lock=ignore_lock,
            )
            self._observer.schedule(handler, path, recursive=True)
            self._watched.append(path)
            logger.info("Watching %s with hooks: %s", path, hook_names)

    def start(self) -> None:
        self._observer.start()
        logger.info("Watcher started (%d path(s))", len(self._watched))

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

        for hook in self._all_hooks:
            try:
                hook.on_shutdown()
            except Exception:
                logger.exception(
                    "Hook %s raised in on_shutdown", type(hook).__name__
                )

        logger.info("Watcher stopped")
