"""Version-vector based 3-way reconciler (spec §3.3, §3.7).

Replaces the legacy 16-cell classify/decide matrix with version compare.

- ``run()``: incremental reconciliation (state file exists).
- ``run_without_state()``: full-list reconciliation (first run / state lost).
- ``decide()``: per-path action decision based on VectorOrdering.
- ``resolve_conflict()``: Syncthing HLC tiebreaker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.config import should_ignore
from src.drive_client import DriveClient
from src.drive_vv_codec import decode as vv_decode
from src.hash import compute_md5
from src.state import FileEntry, SyncState
from src.version_vector import VectorOrdering, VersionVector

logger = logging.getLogger(__name__)


# ── Action types ──────────────────────────────────────────────────────


@dataclass
class NoOp:
    reason: str = ""


@dataclass
class Upload:
    conflict_copy_of: FileEntry | None = None


@dataclass
class Download:
    file_id: str = ""
    conflict_copy_of: FileEntry | None = None


@dataclass
class DeleteRemote:
    file_id: str = ""


@dataclass
class DeleteLocal:
    pass


@dataclass
class UpdateVectorOnly:
    merged: VersionVector | None = None


@dataclass
class AbsorbRemoteTombstone:
    """Copy remote deleted=True to local state only (no file op)."""
    remote_entry: FileEntry | None = None


Action = NoOp | Upload | Download | DeleteRemote | DeleteLocal | UpdateVectorOnly | AbsorbRemoteTombstone


# ── Core decision functions ───────────────────────────────────────────


def decide(
    local: FileEntry | None,
    remote: FileEntry | None,
) -> Action:
    """Decide sync action for a single path (spec §3.3)."""
    if local is None and remote is None:
        return NoOp()

    if local is None:
        return decide_download_or_delete(remote)  # type: ignore[arg-type]

    if remote is None:
        return decide_upload_or_delete(local)

    # Both exist
    # Content identical → vector merge only
    if (
        local.md5 is not None
        and remote.md5 is not None
        and local.md5 == remote.md5
        and local.size == remote.size
    ):
        return UpdateVectorOnly(merged=local.version.merge(remote.version))

    ordering = local.version.compare(remote.version)

    if ordering == VectorOrdering.Equal:
        return NoOp()

    if ordering == VectorOrdering.Greater:
        if local.deleted:
            return DeleteRemote(file_id=remote.drive_id or "")
        return Upload()

    if ordering == VectorOrdering.Lesser:
        if remote.deleted:
            return DeleteLocal()
        return Download(file_id=remote.drive_id or "")

    # Concurrent
    return resolve_conflict(local, remote)


def decide_download_or_delete(remote: FileEntry) -> Action:
    """Remote-only path: download or absorb tombstone (spec §3.3)."""
    if remote.deleted:
        return AbsorbRemoteTombstone(remote_entry=remote)
    return Download(file_id=remote.drive_id or "")


def decide_upload_or_delete(local: FileEntry) -> Action:
    """Local-only path: upload or no-op for local tombstone (spec §3.3)."""
    if local.deleted:
        return NoOp(reason="local_tombstone_no_remote")
    return Upload()


def resolve_conflict(local: FileEntry, remote: FileEntry) -> Action:
    """Syncthing HLC tiebreaker for concurrent vectors (spec §3.3).

    1. max(version.counters.values()) — higher HLC wins.
    2. HLC tie → device prefix (lexicographic greater loses).
    """
    local_hlc = max(local.version.counters.values()) if local.version.counters else 0
    remote_hlc = max(remote.version.counters.values()) if remote.version.counters else 0

    if local_hlc > remote_hlc:
        # Local wins → upload, preserve remote as conflict copy
        return Upload(conflict_copy_of=remote)
    if remote_hlc > local_hlc:
        # Remote wins → download, preserve local as conflict copy
        return Download(
            file_id=remote.drive_id or "",
            conflict_copy_of=local,
        )

    # HLC tie → device prefix comparison (larger prefix loses)
    local_dev = max(local.version.counters.keys()) if local.version.counters else ""
    remote_dev = max(remote.version.counters.keys()) if remote.version.counters else ""
    if local_dev > remote_dev:
        # Local prefix is larger → local loses → download remote
        return Download(
            file_id=remote.drive_id or "",
            conflict_copy_of=local,
        )
    return Upload(conflict_copy_of=remote)


# ── Reconciler class ─────────────────────────────────────────────────


class Reconciler:
    """Reconciles local and remote state using version vectors."""

    def __init__(
        self,
        state: SyncState,
        drive: DriveClient,
        *,
        hash_max_bytes: int | None = None,
    ) -> None:
        self._state = state
        self._drive = drive
        self._hash_max_bytes = hash_max_bytes

    # ── run (state file exists) ──────────────────────────────────────

    def run(self) -> list[dict]:
        """Incremental reconciliation using version compare."""
        old_files: dict[str, FileEntry] = dict(self._state.files)
        new_files: dict[str, FileEntry] = self._state.scan_local_files()

        # Detect local changes by comparing old vs new
        local_changes = self._detect_local_changes(old_files, new_files)

        # Get remote changes
        remote_changes, new_token = self._classify_remote()

        # Build actions
        actions: list[dict] = []
        all_paths = sorted(set(local_changes) | set(remote_changes))

        for path in all_paths:
            if should_ignore(path):
                continue

            local_entry = local_changes.get(path)
            remote_info = remote_changes.get(path)

            action = self._decide_incremental(
                path, local_entry, remote_info, old_files, new_files
            )
            if action is not None:
                actions.append(action)

        if new_token:
            self._state.page_token = new_token

        logger.info(
            f"reconciler.run: local changes {len(local_changes)}, "
            f"remote changes {len(remote_changes)} -> {len(actions)} actions"
        )
        return actions

    def _detect_local_changes(
        self,
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
    ) -> dict[str, dict[str, Any]]:
        """Detect local file changes between old and new scan."""
        changes: dict[str, dict[str, Any]] = {}
        old_keys = set(old_files)
        new_keys = set(new_files)

        for p in new_keys - old_keys:
            changes[p] = {"kind": "new", "entry": new_files[p]}
        for p in old_keys - new_keys:
            if not old_files[p].deleted:
                changes[p] = {"kind": "deleted", "entry": old_files[p]}
        for p in old_keys & new_keys:
            old_e = old_files[p]
            new_e = new_files[p]
            if old_e.mtime != new_e.mtime or old_e.size != new_e.size:
                changes[p] = {"kind": "modified", "entry": new_files[p]}

        return changes

    def _decide_incremental(
        self,
        path: str,
        local_change: dict[str, Any] | None,
        remote_info: dict[str, Any] | None,
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
    ) -> dict | None:
        """Decide action for a single path during incremental sync."""
        # Build local FileEntry
        local_entry: FileEntry | None = None
        if local_change is not None:
            kind = local_change["kind"]
            entry = local_change["entry"]
            if kind in ("new", "modified"):
                # Compute md5 for the local file
                local_abs = self._state.vault_path / path
                md5 = compute_md5(local_abs, max_bytes=self._hash_max_bytes)
                new_version = entry.version.update(self._state.device_id)
                local_entry = FileEntry(
                    mtime=entry.mtime,
                    size=entry.size,
                    drive_id=entry.drive_id,
                    version=new_version,
                    md5=md5,
                )
            elif kind == "deleted":
                new_version = entry.version.update(self._state.device_id)
                local_entry = FileEntry(
                    mtime=entry.mtime,
                    size=entry.size,
                    drive_id=entry.drive_id,
                    version=new_version,
                    deleted=True,
                )
        else:
            # No local change — use existing state
            existing = new_files.get(path) or old_files.get(path)
            if existing is not None:
                local_entry = existing

        # Build remote FileEntry
        remote_entry: FileEntry | None = None
        if remote_info is not None:
            remote_entry = self._remote_info_to_entry(remote_info, path)
        elif local_entry is not None and local_entry.deleted and local_entry.drive_id:
            # Local deleted + no remote change → remote still exists in its last state
            old_entry = old_files.get(path)
            if old_entry is not None and old_entry.drive_id:
                remote_entry = FileEntry(
                    mtime=old_entry.mtime,
                    size=old_entry.size,
                    drive_id=old_entry.drive_id,
                    version=old_entry.version,  # unchanged remote version
                )

        if local_entry is None and remote_entry is None:
            return None

        action = decide(local_entry, remote_entry)
        return self._action_to_dict(action, path, local_entry, remote_entry)

    def _remote_info_to_entry(
        self, remote_info: dict[str, Any], path: str
    ) -> FileEntry | None:
        """Convert raw remote change info to FileEntry."""
        kind = remote_info.get("kind", "")
        file_id = remote_info.get("file_id", "")
        file_meta = remote_info.get("file")

        if kind == "deleted":
            # Remote file was deleted/removed
            existing = self._state.files.get(path)
            old_version = existing.version if existing else VersionVector.empty()
            return FileEntry(
                mtime=0.0,
                size=0,
                drive_id=file_id,
                version=old_version.update("_remote_"),  # different device
                deleted=True,
            )

        if file_meta is None:
            return None

        # Parse appProperties for version vector
        app_props = file_meta.get("appProperties")
        remote_vv, deleted, remote_md5 = vv_decode(app_props)
        if not remote_vv:
            remote_vv = VersionVector.empty()

        remote_mtime = _parse_rfc3339(file_meta.get("modifiedTime"))
        md5 = file_meta.get("md5Checksum") or remote_md5

        return FileEntry(
            mtime=remote_mtime,
            size=int(file_meta.get("size", 0)),
            drive_id=file_id,
            version=remote_vv,
            deleted=deleted,
            md5=md5,
        )

    # ── run_without_state (first run / state lost) ───────────────────

    def run_without_state(self) -> list[dict]:
        """Full-list reconciliation (spec §3.7).

        Five branches:
        1. Both exist + md5 match → no transfer, vector merge.
        2. Local only → upload.
        3. Remote only → download.
        4. Both exist + md5 differ + local.version==empty → forced Conflict (P0 1-B).
        5. Tombstone only → absorb deleted=True.
        """
        remote_files = self._drive.list_all_files()
        remote_by_path: dict[str, dict[str, Any]] = {}
        tombstone_by_path: dict[str, dict[str, Any]] = {}

        for item in remote_files:
            rel_path = item.get("relative_path", item.get("name", ""))
            if should_ignore(rel_path):
                continue
            # Check if this is a tombstone
            app_props = item.get("appProperties")
            if app_props:
                _, deleted, _ = vv_decode(app_props)
                if deleted:
                    tombstone_by_path[rel_path] = item
                    continue
            remote_by_path[rel_path] = item

        local_files = self._state.scan_local_files()

        actions: list[dict] = []
        all_paths = sorted(set(local_files) | set(remote_by_path) | set(tombstone_by_path))

        for path in all_paths:
            local_entry = local_files.get(path)
            remote_meta = remote_by_path.get(path)
            tombstone_meta = tombstone_by_path.get(path)

            action = self._decide_without_state(
                path, local_entry, remote_meta, tombstone_meta
            )
            if action is not None:
                actions.append(action)

        # Issue new page_token
        token = self._drive.get_initial_token()
        self._state.page_token = token

        logger.info(
            f"reconciler.run_without_state: local {len(local_files)}, "
            f"remote {len(remote_by_path)}, tombstones {len(tombstone_by_path)} "
            f"-> {len(actions)} actions, new_token={token}"
        )
        return actions

    def _decide_without_state(
        self,
        path: str,
        local_entry: FileEntry | None,
        remote_meta: dict[str, Any] | None,
        tombstone_meta: dict[str, Any] | None,
    ) -> dict | None:
        """Per-path decision for run_without_state (spec §3.7)."""
        if local_entry is None and remote_meta is None and tombstone_meta is None:
            return None

        # Branch 5: Tombstone only (no local, no active remote)
        if local_entry is None and remote_meta is None and tombstone_meta is not None:
            return self._absorb_tombstone(path, tombstone_meta)

        # Branch 3: Remote only → download
        if local_entry is None and remote_meta is not None:
            file_id = remote_meta["id"]
            # Register drive_id in state
            app_props = remote_meta.get("appProperties")
            remote_vv, _, remote_md5 = vv_decode(app_props)
            self._state.files[path] = FileEntry(
                mtime=0.0,
                size=0,
                drive_id=file_id,
                version=remote_vv if remote_vv else VersionVector.empty(),
                md5=remote_md5,
            )
            return {
                "type": "download",
                "file_id": file_id,
                "path": path,
                "reason": "init_remote_only",
            }

        # Branch 2: Local only (no remote, maybe tombstone)
        if local_entry is not None and remote_meta is None:
            if tombstone_meta is not None:
                # Local exists but remote tombstone → compare versions
                app_props = tombstone_meta.get("appProperties")
                remote_vv, _, _ = vv_decode(app_props)
                if remote_vv and local_entry.version.compare(remote_vv) == VectorOrdering.Lesser:
                    # Tombstone wins → delete local
                    return {
                        "type": "delete_local",
                        "path": path,
                        "reason": "init_tombstone_wins",
                    }
            return {
                "type": "upload",
                "path": path,
                "reason": "init_local_only",
            }

        # Both exist
        if local_entry is not None and remote_meta is not None:
            file_id = remote_meta["id"]
            app_props = remote_meta.get("appProperties")
            remote_vv, _, remote_md5 = vv_decode(app_props)
            drive_md5 = remote_meta.get("md5Checksum") or remote_md5

            # Compute local md5
            local_abs = self._state.vault_path / path
            local_md5 = compute_md5(local_abs, max_bytes=self._hash_max_bytes)

            # Branch 1: md5 match → no transfer, merge vectors
            if local_md5 is not None and drive_md5 is not None and local_md5 == drive_md5:
                merged = local_entry.version.merge(
                    remote_vv if remote_vv else VersionVector.empty()
                )
                self._state.files[path] = FileEntry(
                    mtime=local_entry.mtime,
                    size=local_entry.size,
                    drive_id=file_id,
                    version=merged,
                    md5=local_md5,
                )
                return None  # No transfer needed

            # Branch 4: md5 differ + local.version==empty → forced Conflict (P0 1-B)
            if not local_entry.version.counters:
                # State was lost — force conflict to protect local edits
                self._state.files[path] = FileEntry(
                    mtime=local_entry.mtime,
                    size=local_entry.size,
                    drive_id=file_id,
                    version=local_entry.version,
                    md5=local_md5,
                )
                return {
                    "type": "conflict",
                    "path": path,
                    "local": {
                        "mtime": local_entry.mtime,
                        "size": local_entry.size,
                    },
                    "remote": {
                        "file_id": file_id,
                        "md5": drive_md5,
                    },
                    "reason": "init_state_lost_conflict",
                }

            # Both exist + md5 differ + local.version != empty → normal decide
            remote_entry = FileEntry(
                mtime=_parse_rfc3339(remote_meta.get("modifiedTime")),
                size=int(remote_meta.get("size", 0)),
                drive_id=file_id,
                version=remote_vv if remote_vv else VersionVector.empty(),
                md5=drive_md5,
            )
            local_with_md5 = FileEntry(
                mtime=local_entry.mtime,
                size=local_entry.size,
                drive_id=local_entry.drive_id or file_id,
                version=local_entry.version,
                md5=local_md5,
            )
            action = decide(local_with_md5, remote_entry)
            result = self._action_to_dict(action, path, local_with_md5, remote_entry)

            # Always register drive_id
            self._state.files[path] = FileEntry(
                mtime=local_entry.mtime,
                size=local_entry.size,
                drive_id=file_id,
                version=local_entry.version,
                md5=local_md5,
            )
            return result

        return None

    def _absorb_tombstone(
        self, path: str, tombstone_meta: dict[str, Any]
    ) -> dict | None:
        """Record tombstone in local state without file operations."""
        app_props = tombstone_meta.get("appProperties")
        remote_vv, _, _ = vv_decode(app_props)
        file_id = tombstone_meta.get("id", "")

        self._state.files[path] = FileEntry(
            mtime=0.0,
            size=0,
            drive_id=file_id,
            version=remote_vv if remote_vv else VersionVector.empty(),
            deleted=True,
        )
        logger.debug(f"Absorbed remote tombstone: {path}")
        return None  # No file operation needed

    # ── Remote classification ────────────────────────────────────────

    def _classify_remote(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Classify remote changes from Changes API."""
        token = self._state.page_token
        if not token:
            return {}, None

        changes, new_token = self._drive.get_changes(token)

        remote_kinds: dict[str, dict[str, Any]] = {}
        for change in changes:
            file_id: str = change["file_id"]
            removed: bool = change.get("removed", False)
            file_meta: dict[str, Any] | None = change.get("file")

            known_path = self._path_by_drive_id(file_id)

            if removed:
                if known_path is not None:
                    remote_kinds[known_path] = {
                        "kind": "deleted",
                        "file_id": file_id,
                        "file": None,
                    }
                continue

            if file_meta is None:
                continue

            # Apply IGNORE_PATTERNS to remote changes
            rel_path = known_path or file_meta.get("name", "")
            if should_ignore(rel_path):
                continue

            if known_path is not None:
                remote_kinds[known_path] = {
                    "kind": "modified",
                    "file_id": file_id,
                    "file": file_meta,
                }
            else:
                name = file_meta.get("name")
                if not name:
                    continue
                remote_kinds[name] = {
                    "kind": "new",
                    "file_id": file_id,
                    "file": file_meta,
                }

        return remote_kinds, new_token

    def _path_by_drive_id(self, file_id: str) -> str | None:
        for path, entry in self._state.files.items():
            if entry.drive_id == file_id:
                return path
        return None

    # ── Action dict conversion ───────────────────────────────────────

    def _action_to_dict(
        self,
        action: Action,
        path: str,
        local_entry: FileEntry | None,
        remote_entry: FileEntry | None,
    ) -> dict | None:
        """Convert typed Action to legacy dict format for sync_engine."""
        if isinstance(action, NoOp):
            return None

        if isinstance(action, Upload):
            d: dict[str, Any] = {"type": "upload", "path": path, "reason": "version_greater"}
            if action.conflict_copy_of is not None:
                d["type"] = "conflict"
                d["local"] = _entry_to_info(local_entry)
                d["remote"] = _entry_to_info(remote_entry)
                d["winner"] = "local"
            return d

        if isinstance(action, Download):
            file_id = action.file_id or (remote_entry.drive_id if remote_entry else "")
            d = {
                "type": "download",
                "file_id": file_id,
                "path": path,
                "reason": "version_lesser",
            }
            if action.conflict_copy_of is not None:
                d["type"] = "conflict"
                d["local"] = _entry_to_info(local_entry)
                d["remote"] = _entry_to_info(remote_entry)
                d["winner"] = "remote"
            return d

        if isinstance(action, DeleteRemote):
            file_id = action.file_id or (remote_entry.drive_id if remote_entry else "")
            return {
                "type": "delete_remote",
                "file_id": file_id,
                "path": path,
                "reason": "local_deleted_greater",
            }

        if isinstance(action, DeleteLocal):
            return {
                "type": "delete_local",
                "path": path,
                "reason": "remote_deleted_lesser",
            }

        if isinstance(action, UpdateVectorOnly):
            # Update state with merged vector, no file transfer
            if action.merged and local_entry is not None:
                self._state.update_file(
                    path,
                    FileEntry(
                        mtime=local_entry.mtime,
                        size=local_entry.size,
                        drive_id=local_entry.drive_id
                        or (remote_entry.drive_id if remote_entry else None),
                        version=action.merged,
                        md5=local_entry.md5,
                    ),
                )
            return None

        if isinstance(action, AbsorbRemoteTombstone):
            # Record tombstone in local state
            if action.remote_entry is not None:
                self._state.update_file(
                    path,
                    FileEntry(
                        mtime=0.0,
                        size=0,
                        drive_id=action.remote_entry.drive_id,
                        version=action.remote_entry.version,
                        deleted=True,
                    ),
                )
            return None

        return None


def _entry_to_info(entry: FileEntry | None) -> dict[str, Any]:
    """Convert FileEntry to info dict for conflict action."""
    if entry is None:
        return {}
    return {
        "mtime": entry.mtime,
        "size": entry.size,
        "drive_id": entry.drive_id,
        "md5": entry.md5,
        "file_id": entry.drive_id,
    }


def _parse_rfc3339(value: str | None) -> float:
    """RFC3339 string to UNIX epoch seconds. Returns 0.0 on failure."""
    if not value:
        return 0.0
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        logger.warning(f"RFC3339 parse failed: {value}")
        return 0.0
