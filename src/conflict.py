"""Syncthing-style conflict copy creation.

Naming convention (Syncthing BEP):
    {stem}.sync-conflict-{YYYYMMDD-HHMMSS}-{device_prefix}.{ext}

The winner keeps the original path; the loser is renamed to the
conflict copy. The caller (sync_engine) decides who wins via
resolve_conflict() in reconciler_v2.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Return value constants
CONFLICT_CREATED = "conflict_created"
AUTO_RESOLVED = "auto_resolved"


class ConflictResolver:
    """Creates conflict copies preserving both local and remote versions.

    - Local version is copied to a Syncthing-style conflict filename.
    - The original path is then overwritten by the caller with the winning version.
    """

    def __init__(self, device_id: str, vault_path: Path) -> None:
        self._device_id = device_id
        self._device_prefix = device_id[:8]
        self._vault_path = vault_path

    def resolve(
        self,
        path: str,
        local_info: dict,
        remote_info: dict,
    ) -> str:
        """Create a conflict copy of the local file.

        Args:
            path: Vault-relative path (POSIX).
            local_info: Local metadata (mtime, size, etc.).
            remote_info: Remote metadata (file_id, modified_time, etc.).

        Returns:
            CONFLICT_CREATED or AUTO_RESOLVED.
        """
        local_abs = self._vault_path / path
        if not local_abs.exists():
            logger.warning(f"Conflict target missing: {path}")
            return AUTO_RESOLVED

        conflict_path = self._build_conflict_path(path)
        conflict_abs = self._vault_path / conflict_path

        conflict_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_abs), str(conflict_abs))

        logger.info(
            f"Conflict copy: {path} -> {conflict_path} "
            f"(local_info keys={sorted(local_info)}, remote_info keys={sorted(remote_info)})"
        )
        return CONFLICT_CREATED

    def _build_conflict_path(self, path: str) -> str:
        """Build Syncthing-style conflict filename.

        Format: {stem}.sync-conflict-{YYYYMMDD-HHMMSS}-{device_prefix}.{ext}
        If the file has no extension, no trailing dot is added.
        Uniqueness is guaranteed by incrementing seconds on collision.
        """
        p = Path(path)
        parent = p.parent
        stem = p.stem
        ext = p.suffix  # includes dot, or "" if none

        now = time.time()

        attempts = 0
        while True:
            ts = datetime.fromtimestamp(now + attempts).strftime("%Y%m%d-%H%M%S")
            conflict_name = f"{stem}.sync-conflict-{ts}-{self._device_prefix}{ext}"
            candidate = (
                (parent / conflict_name).as_posix()
                if str(parent) != "."
                else conflict_name
            )

            candidate_abs = self._vault_path / candidate
            if not candidate_abs.exists():
                return candidate

            attempts += 1
            if attempts > 60:
                micro_ts = datetime.fromtimestamp(now).strftime(
                    "%Y%m%d-%H%M%S-%f"
                )
                fallback_name = (
                    f"{stem}.sync-conflict-{micro_ts}-{self._device_prefix}{ext}"
                )
                fallback_rel = (
                    (parent / fallback_name).as_posix()
                    if str(parent) != "."
                    else fallback_name
                )
                logger.warning(
                    f"Too many conflict name collisions, using micro-suffix: {fallback_rel}"
                )
                return fallback_rel
