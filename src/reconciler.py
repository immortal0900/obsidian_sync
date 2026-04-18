"""재시작 시 로컬/클라우드 상태를 대조하여 밀린 변경을 합친다.

- `run()`: 상태 파일이 있을 때 — 저장된 page_token으로 증분 대조.
- `run_without_state()`: 첫 실행 또는 상태 파일 분실 시 — 전체 목록 대조.

대조 규칙표(spec §5-6)에 따라 action 리스트를 생성한다.
실제 실행은 호출자(SyncEngine)가 담당한다.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.config import should_ignore
from src.drive_client import DriveClient
from src.state import FileEntry, SyncState

logger = logging.getLogger(__name__)

# 로컬 파일 분류
LOCAL_UNCHANGED = "unchanged"
LOCAL_NEW = "new"
LOCAL_MODIFIED = "modified"
LOCAL_DELETED = "deleted"

# 원격 변경 분류
REMOTE_UNCHANGED = "unchanged"
REMOTE_NEW = "new"
REMOTE_MODIFIED = "modified"
REMOTE_DELETED = "deleted"


class Reconciler:
    """재시작 시 양쪽 상태를 합치고 action 리스트를 생성한다."""

    def __init__(self, state: SyncState, drive: DriveClient) -> None:
        self._state = state
        self._drive = drive

    # ── run (상태 파일 존재) ────────────────────────────────────────────

    def run(self) -> list[dict]:
        """증분 대조 알고리즘.

        전제: 호출자가 이미 `state.load()`로 인메모리 상태를 채운 상태.
        """
        old_files: dict[str, FileEntry] = dict(self._state.files)
        new_files: dict[str, FileEntry] = self._state.scan_local_files()

        # 로컬 분류
        local_kinds = self._classify_local(old_files, new_files)

        # 원격 분류
        remote_kinds, new_token = self._classify_remote()

        # 대조 + action 생성
        actions = self._apply_rules(
            old_files, new_files, local_kinds, remote_kinds
        )

        # page_token 갱신 (저장은 execute 이후 호출자가 수행)
        if new_token:
            self._state.page_token = new_token

        logger.info(
            f"reconciler.run: 로컬 변화 {len(local_kinds)}건, "
            f"원격 변화 {len(remote_kinds)}건 → {len(actions)}개 action"
        )
        return actions

    # ── run_without_state (첫 실행/상태 분실) ────────────────────────────

    def run_without_state(self) -> list[dict]:
        """전체 목록 대조로 초기 상태를 구축한다.

        매칭은 볼트 기준 상대 경로(POSIX)로 수행한다.
        """
        remote_files = self._drive.list_all_files()
        # remote_files의 각 항목에는 drive_client가 부여한 relative_path 있음.
        # IGNORE_PATTERNS에 해당하는 원격 파일은 로컬로 내려받지 않는다.
        remote_by_path: dict[str, dict[str, Any]] = {
            item["relative_path"]: item
            for item in remote_files
            if not should_ignore(item["relative_path"])
        }
        local_files = self._state.scan_local_files()

        actions: list[dict] = []
        all_paths = set(local_files) | set(remote_by_path)

        for path in sorted(all_paths):
            local_entry = local_files.get(path)
            remote_meta = remote_by_path.get(path)

            if local_entry is not None and remote_meta is None:
                # 로컬에만 존재 → 업로드
                actions.append(
                    {"type": "upload", "path": path, "reason": "init_local_only"}
                )
            elif local_entry is None and remote_meta is not None:
                # 원격에만 존재 → 다운로드
                actions.append(
                    {
                        "type": "download",
                        "file_id": remote_meta["id"],
                        "path": path,
                        "reason": "init_remote_only",
                    }
                )
            else:
                # 양쪽 존재 → 최신 우선 (재설치 시나리오, 충돌로 간주하지 않음)
                remote_mtime = _parse_rfc3339(remote_meta.get("modifiedTime"))
                local_mtime = local_entry.mtime if local_entry else 0.0
                if remote_mtime > local_mtime:
                    actions.append(
                        {
                            "type": "download",
                            "file_id": remote_meta["id"],
                            "path": path,
                            "reason": "init_remote_newer",
                        }
                    )
                elif local_mtime > remote_mtime:
                    actions.append(
                        {"type": "upload", "path": path, "reason": "init_local_newer"}
                    )
                # 같으면 no-op, 단 state에는 drive_id를 기록해야 함
                # → 호출자가 action 실행 후 state.save()로 반영됨. 여기서는 간단히 갱신.
                if local_entry is not None and remote_meta is not None:
                    # 매칭된 항목은 인메모리 state에 drive_id 반영
                    entry = FileEntry(
                        mtime=local_entry.mtime,
                        size=local_entry.size,
                        drive_id=remote_meta["id"],
                    )
                    self._state.files[path] = entry

        # 새 page_token 발급
        token = self._drive.get_initial_token()
        self._state.page_token = token

        logger.info(
            f"reconciler.run_without_state: 로컬 {len(local_files)}개, "
            f"원격 {len(remote_by_path)}개 → {len(actions)}개 action, "
            f"new_token={token}"
        )
        return actions

    # ── 분류 ──────────────────────────────────────────────────────────────

    def _classify_local(
        self,
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
    ) -> dict[str, str]:
        """로컬 diff를 path → kind 매핑으로 변환한다."""
        diff = self._state.diff(old_files, new_files)
        result: dict[str, str] = {}
        for p in diff.added:
            result[p] = LOCAL_NEW
        for p in diff.modified:
            result[p] = LOCAL_MODIFIED
        for p in diff.deleted:
            result[p] = LOCAL_DELETED
        return result

    def _classify_remote(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """원격 변경을 path → {kind, file_id, file} 매핑으로 변환한다.

        - 기존 drive_id와 일치하면 기존 경로로 매핑 (modified 또는 deleted).
        - 알 수 없는 drive_id + removed=False면 file.name을 path로 사용 (new).
        - 알 수 없는 drive_id + removed=True는 무시 (추적하지 않는 파일의 삭제).

        반환: (remote_kinds, new_page_token).
        """
        token = self._state.page_token
        if not token:
            # 토큰 없음 → 증분 대조 불가 (run_without_state가 호출되어야 함)
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
                        "kind": REMOTE_DELETED,
                        "file_id": file_id,
                        "file": None,
                    }
                # 알 수 없는 파일의 삭제 → 무시
                continue

            if file_meta is None:
                continue

            if known_path is not None:
                if should_ignore(known_path):
                    continue
                remote_kinds[known_path] = {
                    "kind": REMOTE_MODIFIED,
                    "file_id": file_id,
                    "file": file_meta,
                }
            else:
                name = file_meta.get("name")
                if not name:
                    continue
                if should_ignore(name):
                    continue
                remote_kinds[name] = {
                    "kind": REMOTE_NEW,
                    "file_id": file_id,
                    "file": file_meta,
                }

        return remote_kinds, new_token

    def _path_by_drive_id(self, file_id: str) -> str | None:
        """상태의 files에서 drive_id가 일치하는 경로를 찾는다."""
        for path, entry in self._state.files.items():
            if entry.drive_id == file_id:
                return path
        return None

    # ── 규칙 적용 ────────────────────────────────────────────────────────

    def _apply_rules(
        self,
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
        local_kinds: dict[str, str],
        remote_kinds: dict[str, dict[str, Any]],
    ) -> list[dict]:
        """16셀 대조 규칙을 적용해 action 리스트를 만든다."""
        actions: list[dict] = []
        all_paths = sorted(set(local_kinds) | set(remote_kinds))

        for path in all_paths:
            if should_ignore(path):
                continue
            local_kind = local_kinds.get(path, LOCAL_UNCHANGED)
            remote_info = remote_kinds.get(
                path, {"kind": REMOTE_UNCHANGED, "file_id": None, "file": None}
            )
            remote_kind = remote_info["kind"]

            action = self._decide(
                path, local_kind, remote_kind, remote_info, old_files, new_files
            )
            if action is not None:
                actions.append(action)

        return actions

    def _decide(
        self,
        path: str,
        local_kind: str,
        remote_kind: str,
        remote_info: dict[str, Any],
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
    ) -> dict | None:
        """단일 셀의 action을 결정한다 (spec §5-6 그대로).

        표 16셀:
                        | 원격 unchanged | 원격 new  | 원격 modified | 원격 deleted
            ------------|---------------|-----------|--------------|-------------
            로컬 uncg.  | no-op         | download  | download     | no-op
            로컬 new    | upload        | conflict  | n/a          | n/a
            로컬 modif. | upload        | n/a       | conflict     | conflict
            로컬 deleted| delete_remote | n/a       | conflict     | no-op
        """
        file_id = remote_info.get("file_id")

        # 로컬: 변경 없음 ──────────────────────────────────────────────
        if local_kind == LOCAL_UNCHANGED:
            if remote_kind in (REMOTE_NEW, REMOTE_MODIFIED):
                return {
                    "type": "download",
                    "file_id": file_id,
                    "path": path,
                    "reason": f"remote_{remote_kind}",
                }
            if remote_kind == REMOTE_DELETED:
                # decision-003: no-op 유지하되 유령 drive_id 정리.
                # 로컬 파일은 보존 (Policy 1). 다음 로컬 수정 시 재업로드로 복구.
                self._state.remove_file(path)
                return None
            # unchanged × unchanged → no-op
            return None

        # 로컬: 새 파일 ────────────────────────────────────────────────
        if local_kind == LOCAL_NEW:
            if remote_kind == REMOTE_UNCHANGED:
                return {"type": "upload", "path": path, "reason": "local_new"}
            if remote_kind == REMOTE_NEW:
                return self._conflict_action(
                    path, new_files.get(path), remote_info
                )
            # new × (modified|deleted) → n/a
            return None

        # 로컬: 수정됨 ──────────────────────────────────────────────────
        if local_kind == LOCAL_MODIFIED:
            if remote_kind == REMOTE_UNCHANGED:
                return {"type": "upload", "path": path, "reason": "local_modified"}
            if remote_kind in (REMOTE_MODIFIED, REMOTE_DELETED):
                return self._conflict_action(
                    path, new_files.get(path), remote_info
                )
            return None

        # 로컬: 삭제됨 ──────────────────────────────────────────────────
        if local_kind == LOCAL_DELETED:
            if remote_kind == REMOTE_UNCHANGED:
                old_entry = old_files.get(path)
                if old_entry is None or old_entry.drive_id is None:
                    return None
                return {
                    "type": "delete_remote",
                    "file_id": old_entry.drive_id,
                    "path": path,
                    "reason": "local_deleted",
                }
            if remote_kind == REMOTE_MODIFIED:
                return self._conflict_action(path, old_files.get(path), remote_info)
            # deleted × (new|deleted) → no-op / n/a
            return None

        return None

    def _conflict_action(
        self,
        path: str,
        local_entry: FileEntry | None,
        remote_info: dict[str, Any],
    ) -> dict:
        """충돌 action을 생성한다 (sync_engine에서 conflict_resolver로 위임된다)."""
        local_payload: dict[str, Any] = {}
        if local_entry is not None:
            local_payload = {
                "mtime": local_entry.mtime,
                "size": local_entry.size,
                "drive_id": local_entry.drive_id,
            }
        remote_file = remote_info.get("file") or {}
        remote_payload = {
            "file_id": remote_info.get("file_id"),
            "modified_time": remote_file.get("modified_time")
            or remote_file.get("modifiedTime"),
            "md5": remote_file.get("md5") or remote_file.get("md5Checksum"),
            "name": remote_file.get("name"),
        }
        return {
            "type": "conflict",
            "path": path,
            "local": local_payload,
            "remote": remote_payload,
        }


def _parse_rfc3339(value: str | None) -> float:
    """RFC3339 문자열을 UNIX epoch 초로 변환한다. 실패 시 0.0."""
    if not value:
        return 0.0
    try:
        # "2026-04-14T15:30:00.000Z" 또는 "2026-04-14T15:30:00Z"
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        logger.warning(f"RFC3339 파싱 실패: {value}")
        return 0.0
