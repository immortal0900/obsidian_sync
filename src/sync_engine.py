"""동기화 작업 실행 엔진.

reconciler가 생성한 action을 실행하고, 성공 시 상태 파일을 갱신한다.
동시 실행 방지를 위한 잠금(self.lock)을 유지하며,
실행 중 추가 작업이 들어오면 내부 큐에 적재 후 순차 처리한다.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

from src.conflict import ConflictResolver
from src.drive_client import DriveClient, DriveFileNotFoundError, TokenInvalidError
from src.state import FileEntry, SyncState

logger = logging.getLogger(__name__)

# 지원하는 action type
ACTION_UPLOAD = "upload"
ACTION_DOWNLOAD = "download"
ACTION_DELETE_REMOTE = "delete_remote"
ACTION_DELETE_LOCAL = "delete_local"
ACTION_RENAME_REMOTE = "rename_remote"
ACTION_CONFLICT = "conflict"


class SyncEngine:
    """실제 동기화 작업을 실행한다.

    - `execute(action)`는 단일 작업을 실행하고 상태 파일을 갱신한다.
    - `handle_local_change/handle_remote_changes`는 watchdog/poller 연동 지점이다.
    - `_acquire_lock/_release_lock`은 동시 실행을 방지한다.
    """

    def __init__(
        self,
        drive_client: DriveClient,
        state: SyncState,
        conflict_resolver: ConflictResolver,
    ) -> None:
        self._drive = drive_client
        self._state = state
        self._conflict = conflict_resolver

        # 잠금: 실행 중 여부
        self.lock: bool = False
        self._lock_guard = threading.Lock()

        # 잠금 보유 중 들어온 execute 호출을 쌓아두는 큐
        self._pending: deque[dict] = deque()

    # ── 잠금 관리 ────────────────────────────────────────────────────────

    def _acquire_lock(self) -> bool:
        """잠금 획득을 시도한다. 이미 보유 중이면 False."""
        with self._lock_guard:
            if self.lock:
                return False
            self.lock = True
            return True

    def _release_lock(self) -> None:
        """잠금을 해제한다."""
        with self._lock_guard:
            self.lock = False

    # ── 공개 API ─────────────────────────────────────────────────────────

    def execute(self, action: dict) -> None:
        """단일 action을 실행한다.

        이미 실행 중이면 내부 큐에 적재 후 즉시 반환 (재진입 방지).
        실행 중이 아니면 잠금을 획득하고 큐에 쌓인 작업까지 순차 처리한다.
        """
        if not self._acquire_lock():
            # 이미 실행 중 → 큐에 쌓아두고 복귀 (호출자 블로킹 방지)
            self._pending.append(action)
            logger.debug(f"잠금 중 → 작업 큐잉: {action.get('type')}")
            return

        try:
            self._run_action(action)
            while self._pending:
                queued = self._pending.popleft()
                self._run_action(queued)
        finally:
            self._release_lock()

    def handle_local_change(self, event_type: str, path: str) -> None:
        """watchdog 이벤트를 action으로 변환하여 실행한다.

        event_type: "created" | "modified" | "deleted"
        """
        if event_type in {"created", "modified"}:
            action = {"type": ACTION_UPLOAD, "path": path, "reason": f"local_{event_type}"}
        elif event_type == "deleted":
            existing = self._state.files.get(path)
            if existing is None or existing.drive_id is None:
                logger.debug(f"상태에 없는 파일 삭제 이벤트 무시: {path}")
                return
            action = {
                "type": ACTION_DELETE_REMOTE,
                "file_id": existing.drive_id,
                "path": path,
                "reason": "local_deleted",
            }
        else:
            logger.warning(f"지원하지 않는 이벤트 타입: {event_type}")
            return

        self.execute(action)

    def handle_remote_changes(self, changes: list[dict]) -> None:
        """poller 결과(정규화된 Changes 리스트)를 순회 실행한다.

        각 change는 drive_client.get_changes()의 반환 스키마를 따른다:
            {file_id, removed, file: {name, modified_time, md5} | None}
        """
        for change in changes:
            action = self._change_to_action(change)
            if action is not None:
                self.execute(action)

    # ── 내부 구현 ────────────────────────────────────────────────────────

    def _run_action(self, action: dict) -> None:
        """잠금 보유 상태에서 단일 action을 처리한다."""
        action_type = action.get("type")
        try:
            if action_type == ACTION_UPLOAD:
                self._do_upload(action)
            elif action_type == ACTION_DOWNLOAD:
                self._do_download(action)
            elif action_type == ACTION_DELETE_REMOTE:
                self._do_delete_remote(action)
            elif action_type == ACTION_DELETE_LOCAL:
                self._do_delete_local(action)
            elif action_type == ACTION_RENAME_REMOTE:
                self._do_rename_remote(action)
            elif action_type == ACTION_CONFLICT:
                self._do_conflict(action)
            else:
                logger.error(f"알 수 없는 action type: {action_type}")
        except TokenInvalidError:
            # 토큰 무효화는 상위(reconciler)로 전파
            raise
        except DriveFileNotFoundError as e:
            # 404 — 상태 파일에서 해당 drive_id 제거
            self._cleanup_missing_drive_id(e.file_id, action)
        except Exception:
            logger.exception(f"action 실행 실패: {action}")

    def _cleanup_missing_drive_id(self, missing_id: str, action: dict) -> None:
        """404로 사라진 drive_id를 state에서 정리한다."""
        path_hint = action.get("path")
        if path_hint is not None:
            entry = self._state.files.get(path_hint)
            if entry is not None and entry.drive_id == missing_id:
                self._state.remove_file(path_hint)
                logger.info(
                    f"404 정리: state에서 제거 {path_hint} (drive_id={missing_id})"
                )
                return

        # path 힌트로 찾지 못한 경우 drive_id 전체 스캔
        to_remove: list[str] = [
            path
            for path, entry in self._state.files.items()
            if entry.drive_id == missing_id
        ]
        for path in to_remove:
            self._state.remove_file(path)
            logger.info(
                f"404 정리: state에서 제거 {path} (drive_id={missing_id})"
            )

    def _do_upload(self, action: dict) -> None:
        path: str = action["path"]
        local_abs = self._state.vault_path / path
        if not local_abs.exists():
            logger.warning(f"업로드 대상 파일 없음: {path}")
            return

        existing = self._state.files.get(path)
        existing_id = existing.drive_id if existing else None

        drive_id = self._drive.upload(local_abs, path, existing_id=existing_id)

        stat = local_abs.stat()
        self._state.update_file(
            path,
            FileEntry(mtime=stat.st_mtime, size=stat.st_size, drive_id=drive_id),
        )
        logger.info(f"동기화 완료 (upload): {path}")

    def _do_download(self, action: dict) -> None:
        file_id: str = action["file_id"]
        path: str = action["path"]
        local_abs: Path = self._state.vault_path / path

        self._drive.download(file_id, local_abs)

        stat = local_abs.stat()
        self._state.update_file(
            path,
            FileEntry(mtime=stat.st_mtime, size=stat.st_size, drive_id=file_id),
        )
        logger.info(f"동기화 완료 (download): {path}")

    def _do_delete_remote(self, action: dict) -> None:
        file_id: str = action["file_id"]
        path: str | None = action.get("path")

        self._drive.delete(file_id)

        if path is not None:
            self._state.remove_file(path)
        logger.info(f"동기화 완료 (delete_remote): {path or file_id}")

    def _do_delete_local(self, action: dict) -> None:
        path: str = action["path"]
        local_abs: Path = self._state.vault_path / path

        if local_abs.exists():
            try:
                local_abs.unlink()
            except OSError:
                logger.exception(f"로컬 파일 삭제 실패: {path}")
                return

        self._state.remove_file(path)
        logger.info(f"동기화 완료 (delete_local): {path}")

    def _do_rename_remote(self, action: dict) -> None:
        old_path: str = action["old_path"]
        new_path: str = action["new_path"]

        existing = self._state.files.get(old_path)
        if existing is None or existing.drive_id is None:
            logger.warning(f"rename 대상이 상태에 없음: {old_path}")
            return

        new_name = new_path.rsplit("/", 1)[-1]
        self._drive.rename(existing.drive_id, new_name)

        # state key 교체
        self._state.remove_file(old_path)
        self._state.update_file(new_path, existing)
        logger.info(f"동기화 완료 (rename_remote): {old_path} → {new_path}")

    def _do_conflict(self, action: dict) -> None:
        path: str = action["path"]
        local_info: dict = action.get("local", {})
        remote_info: dict = action.get("remote", {})

        # 충돌 사본 생성 (로컬 보존)
        self._conflict.resolve(path, local_info, remote_info)

        # 원본은 클라우드 버전으로 덮어쓰기
        file_id = remote_info.get("file_id")
        if file_id:
            download_action = {
                "type": ACTION_DOWNLOAD,
                "file_id": file_id,
                "path": path,
                "reason": "conflict_remote_wins",
            }
            self._do_download(download_action)
        else:
            logger.warning(f"충돌 처리 중 remote file_id 없음: {path}")

    def _change_to_action(self, change: dict) -> dict | None:
        """단일 원격 변경을 action으로 변환한다 (상태 파일과 대조)."""
        file_id: str = change["file_id"]
        removed: bool = change.get("removed", False)
        file_meta: dict[str, Any] | None = change.get("file")

        # drive_id → 기존 경로 역매핑
        existing_path = self._find_path_by_drive_id(file_id)

        if removed:
            if existing_path is None:
                # 볼트에 없는 삭제 → 무시
                return None
            return {
                "type": ACTION_DELETE_LOCAL,
                "path": existing_path,
                "reason": "remote_removed",
            }

        # removed=False이지만 file 없음 → 건너뜀
        if file_meta is None:
            return None

        name = file_meta.get("name")
        if not name:
            return None

        if existing_path is not None:
            # 이미 알고 있는 파일 → 재다운로드
            return {
                "type": ACTION_DOWNLOAD,
                "file_id": file_id,
                "path": existing_path,
                "reason": "remote_modified",
            }

        # 새 파일 → 루트 폴더 기준 이름으로 다운로드 (중첩 폴더는 reconciler에서 처리)
        return {
            "type": ACTION_DOWNLOAD,
            "file_id": file_id,
            "path": name,
            "reason": "remote_new",
        }

    def _find_path_by_drive_id(self, file_id: str) -> str | None:
        """drive_id로 상태 파일에서 해당 경로를 찾는다."""
        for path, entry in self._state.files.items():
            if entry.drive_id == file_id:
                return path
        return None
