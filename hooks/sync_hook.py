from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ChangeEvent:
    """
    Normalized file-change event passed to all hooks.
    Decouples hooks from watchdog internals so future hooks
    (blog_hook, llm_hook) don't need to import watchdog at all.
    """

    event_type: str        # "created" | "modified" | "deleted" | "moved"
    src_path: Path
    dest_path: Path | None  # only populated for "moved" events
    is_directory: bool


class BaseHook:
    """
    Abstract base for all hooks.
    Phase 2/3 hooks inherit from this and override on_change().
    """

    def on_change(self, event: ChangeEvent) -> None:
        raise NotImplementedError

    def on_shutdown(self) -> None:
        """Called when daemon is stopping. Override for cleanup."""
        pass


class SyncHook(BaseHook):
    """
    Phase 1 hook: uploads local changes to Google Drive.
    Drive→local polling is handled separately inside DriveSync.
    """

    def __init__(self, drive_sync, **kwargs):
        # DriveSync instance injected by VaultWatcher via load_hooks()
        self._drive = drive_sync

    def on_change(self, event: ChangeEvent) -> None:
        try:
            if event.event_type == "deleted":
                if not event.is_directory:
                    self._drive.delete_file(event.src_path)
            elif event.event_type == "moved":
                self._drive.move_file(event.src_path, event.dest_path)
            elif event.event_type in ("created", "modified"):
                if not event.is_directory:
                    self._drive.upload_file(event.src_path)
                else:
                    self._drive.ensure_folder(event.src_path)
        except Exception:
            logger.exception(
                "SyncHook.on_change failed for %s", event.src_path
            )
