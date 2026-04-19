"""동기화 작업 실행 엔진.

reconciler가 생성한 action을 실행하고, 성공 시 상태 파일을 갱신한다.
동시 실행 방지를 위한 잠금(self.lock)을 유지하며,
실행 중 추가 작업이 들어오면 내부 큐에 적재 후 순차 처리한다.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from src.config import should_ignore
from src.conflict import ConflictResolver
from src.drive_client import DriveClient, DriveFileNotFoundError, TokenInvalidError
from src.state import FileEntry, SyncState
from src.trash import TrashManager
from src.version_vector import VersionVector

logger = logging.getLogger(__name__)

# 지원하는 action type
ACTION_UPLOAD = "upload"
ACTION_DOWNLOAD = "download"
ACTION_DELETE_REMOTE = "delete_remote"
ACTION_DELETE_LOCAL = "delete_local"
ACTION_RENAME_REMOTE = "rename_remote"
ACTION_CONFLICT = "conflict"

# 에코 억제 윈도우 — 우리가 방금 쓴 로컬 경로 / 업로드한 drive_id는 이 시간 동안 무시
ECHO_SUPPRESS_WINDOW_SECONDS = 15.0


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
        trash_manager: TrashManager | None = None,
        intent_log: Any | None = None,
    ) -> None:
        self._drive = drive_client
        self._state = state
        self._conflict = conflict_resolver
        self._trash = trash_manager
        self._intent_log = intent_log

        # 잠금: 실행 중 여부
        self.lock: bool = False
        self._lock_guard = threading.Lock()

        # 잠금 보유 중 들어온 execute 호출을 쌓아두는 큐
        self._pending: deque[dict] = deque()

        # 에코 억제 — (path → 만료 monotonic), (drive_id → 만료 monotonic)
        self._recent_local_writes: dict[str, float] = {}
        self._recent_drive_writes: dict[str, float] = {}

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

    # ── 에코 억제 ────────────────────────────────────────────────────────

    def _mark_local_written(self, path: str) -> None:
        """우리가 방금 이 로컬 경로를 썼음을 기록 (watcher echo 방지)."""
        self._recent_local_writes[path] = (
            time.monotonic() + ECHO_SUPPRESS_WINDOW_SECONDS
        )

    def _mark_drive_written(self, drive_id: str) -> None:
        """우리가 방금 이 drive_id를 업로드했음을 기록 (poller echo 방지)."""
        self._recent_drive_writes[drive_id] = (
            time.monotonic() + ECHO_SUPPRESS_WINDOW_SECONDS
        )

    def _is_echo_local(self, path: str) -> bool:
        """방금 우리가 쓴 경로의 watcher 이벤트인지 확인."""
        deadline = self._recent_local_writes.get(path)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._recent_local_writes.pop(path, None)
            return False
        return True

    def _is_echo_drive(self, drive_id: str) -> bool:
        """방금 우리가 업로드한 drive_id의 poller 이벤트인지 확인."""
        deadline = self._recent_drive_writes.get(drive_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._recent_drive_writes.pop(drive_id, None)
            return False
        return True

    # ── 공개 API ─────────────────────────────────────────────────────────

    def replay_intents(self) -> int:
        """Boot-time replay of unresolved intents from the WAL.

        Returns the number of intents replayed.
        """
        if self._intent_log is None:
            return 0
        return self._intent_log.replay(self._run_action)

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
        if self._is_echo_local(path):
            logger.debug(f"echo 억제 (local_write): {path}")
            return

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
        """잠금 보유 상태에서 단일 action을 처리한다.

        Intent Log WAL: record before execution, resolve on success.
        """
        action_type = action.get("type")

        # WAL: record intent before execution
        intent_id: str | None = None
        if self._intent_log is not None:
            intent_id = self._intent_log.record(action)

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
                return

            # WAL: resolve on success
            if self._intent_log is not None and intent_id is not None:
                self._intent_log.resolve(intent_id)

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
        from src.drive_vv_codec import encode as vv_encode
        from src.hash import compute_md5

        path: str = action["path"]
        local_abs = self._state.vault_path / path
        if not local_abs.exists():
            logger.warning(f"업로드 대상 파일 없음: {path}")
            return

        existing = self._state.files.get(path)
        existing_id = existing.drive_id if existing else None
        old_version = existing.version if existing else VersionVector.empty()

        # Compute local md5
        local_md5 = compute_md5(local_abs)

        # Echo guard: state에 이미 동일 md5+size로 기록돼 있으면
        # 이는 우리가 방금 download/upload한 파일의 watcher 반향이므로 skip.
        if existing is not None and existing.md5 is not None \
                and existing.md5 == local_md5 \
                and existing.size == local_abs.stat().st_size \
                and not existing.deleted:
            logger.debug(f"echo 억제 (md5 동일): {path}")
            return

        # Defensive: existing_id가 없는데 Drive에 같은 경로 파일이 있으면
        # 중복 생성 방지를 위해 그 drive_id를 재사용 (PR4 후속 핫픽스).
        if existing_id is None:
            try:
                found_id = self._drive.find_file_by_rel_path(path)
            except Exception:
                logger.warning(
                    f"Drive 중복 조회 실패 — 새 파일로 업로드 계속: {path}",
                    exc_info=True,
                )
                found_id = None
            if found_id is not None:
                logger.info(
                    f"중복 생성 방지: Drive에 기존 파일 발견 → 재사용 "
                    f"(path={path}, drive_id={found_id})"
                )
                existing_id = found_id

        new_version = old_version.update(self._state.device_id)

        # appProperties에 version vector 인코딩
        app_props = vv_encode(new_version, deleted=False, md5=local_md5)

        result = self._drive.upload(
            local_abs, path, existing_id=existing_id, app_properties=app_props
        )
        drive_id = result["id"] if isinstance(result, dict) else result

        # Drive API가 반환한 md5Checksum 저장
        drive_md5 = result.get("md5Checksum") if isinstance(result, dict) else None

        stat = local_abs.stat()
        self._state.update_file(
            path,
            FileEntry(
                mtime=stat.st_mtime,
                size=stat.st_size,
                drive_id=drive_id,
                version=new_version,
                md5=drive_md5 or local_md5,
            ),
        )
        self._mark_drive_written(drive_id)
        logger.info(f"동기화 완료 (upload): {path}")

    def _do_download(self, action: dict) -> None:
        from src.drive_vv_codec import decode as vv_decode
        from src.hash import compute_md5

        file_id: str = action["file_id"]
        path: str = action["path"]
        local_abs: Path = self._state.vault_path / path

        # 3-way md5 비교 가드 (v2 핫픽스):
        # 로컬 md5(L) / state md5(S) / Drive md5(D) 3개를 비교해 과잉 conflict 방지.
        #   L==S==D            -> 셋 다 같음, download 불필요 → skip
        #   L==S, S!=D         -> Drive만 갱신 → 정상 download
        #   L!=S, L==D         -> 로컬=Drive (외부 자동저장 후 state만 오래됨)
        #                         → download 불필요, state의 version/md5만 갱신
        #   L!=S, L!=D         -> 진짜 동시 편집 → conflict 사본 + download
        if local_abs.exists():
            state_entry = self._state.files.get(path)
            current_local_md5 = compute_md5(local_abs)

            remote_md5: str | None = None
            try:
                meta_pre = self._drive.get_file_metadata(
                    file_id, fields="md5Checksum"
                )
                remote_md5 = (
                    meta_pre.get("md5Checksum") if isinstance(meta_pre, dict) else None
                )
            except Exception:
                logger.warning(
                    f"download 전 Drive md5 조회 실패 — 가드 비활성화, 정상 download 진행: {path}"
                )

            if current_local_md5 and remote_md5:
                state_md5 = state_entry.md5 if state_entry else None
                local_eq_state = state_md5 is not None and current_local_md5 == state_md5
                local_eq_remote = current_local_md5 == remote_md5

                if local_eq_state and local_eq_remote:
                    logger.debug(f"download skip (all 3 md5 equal): {path}")
                    return
                if not local_eq_state and local_eq_remote:
                    # 로컬이 이미 Drive와 같음 → download 불필요. state만 최신화.
                    logger.info(
                        f"download skip (local == remote md5, state stale only): {path}"
                    )
                    if state_entry is not None:
                        self._state.update_file(
                            path,
                            FileEntry(
                                mtime=local_abs.stat().st_mtime,
                                size=local_abs.stat().st_size,
                                drive_id=state_entry.drive_id or file_id,
                                version=state_entry.version,
                                md5=current_local_md5,
                                deleted=False,
                            ),
                        )
                    return
                if not local_eq_state and not local_eq_remote:
                    # 진짜 동시 편집 → 로컬본 보존 후 Drive 버전 download
                    logger.warning(
                        f"concurrent edit detected (L!=S, L!=D) → conflict 사본 보존: {path}"
                    )
                    try:
                        self._conflict.resolve(
                            path,
                            local_info={"md5": current_local_md5},
                            remote_info={"file_id": file_id, "md5": remote_md5},
                        )
                    except Exception:
                        logger.exception(
                            f"충돌 사본 생성 실패 — download 중단: {path}"
                        )
                        return
                # L==S, S!=D 인 경우(Drive만 갱신) → 아래 정상 download 흐름으로 진행

        self._mark_local_written(path)
        meta = self._drive.download(file_id, local_abs)

        stat = local_abs.stat()

        # 원격 vector를 반영: appProperties에서 디코딩
        app_props = meta.get("appProperties") if isinstance(meta, dict) else None
        remote_vv, _deleted, remote_md5 = vv_decode(app_props)

        # 원격 vector가 있으면 그대로 사용, 없으면 로컬 갱신 (legacy 파일 대응)
        if remote_vv:
            new_version = remote_vv
        else:
            existing = self._state.files.get(path)
            old_version = existing.version if existing else VersionVector.empty()
            new_version = old_version.update(self._state.device_id)

        # Drive API md5Checksum + local compute_md5
        drive_md5 = meta.get("md5Checksum") if isinstance(meta, dict) else None
        local_md5 = compute_md5(local_abs)

        self._state.update_file(
            path,
            FileEntry(
                mtime=stat.st_mtime,
                size=stat.st_size,
                drive_id=file_id,
                version=new_version,
                md5=drive_md5 or local_md5 or remote_md5,
            ),
        )
        logger.info(f"동기화 완료 (download): {path}")

    def _do_delete_remote(self, action: dict) -> None:
        from src.drive_vv_codec import encode as vv_encode

        file_id: str = action["file_id"]
        path: str | None = action.get("path")

        if path is not None:
            existing = self._state.files.get(path)
            old_version = existing.version if existing else VersionVector.empty()
            new_version = old_version.update(self._state.device_id)

            # tombstone move + appProperties 갱신
            app_props = vv_encode(new_version, deleted=True)
            self._drive.move_to_tombstones(file_id, app_properties=app_props)

            self._state.update_file(
                path,
                FileEntry(
                    mtime=existing.mtime if existing else 0.0,
                    size=existing.size if existing else 0,
                    drive_id=file_id,
                    version=new_version,
                    deleted=True,
                    deleted_at=time.time(),
                ),
            )
        else:
            # path 없는 경우 fallback: hard_delete
            self._drive.hard_delete(file_id)
        logger.info(f"동기화 완료 (delete_remote): {path or file_id}")

    def _do_delete_local(self, action: dict) -> None:
        path: str = action["path"]
        local_abs: Path = self._state.vault_path / path

        self._mark_local_written(path)
        if local_abs.exists():
            if self._trash is not None:
                try:
                    existing = self._state.files.get(path)
                    md5 = existing.md5 if existing else None
                    self._trash.move(local_abs, path, md5=md5)
                except Exception:
                    logger.exception(f"trash 이동 실패, 직접 삭제 시도: {path}")
                    try:
                        local_abs.unlink()
                    except OSError:
                        logger.exception(f"로컬 파일 삭제 실패: {path}")
                        return
            else:
                try:
                    local_abs.unlink()
                except OSError:
                    logger.exception(f"로컬 파일 삭제 실패: {path}")
                    return

        # version 갱신 + deleted 마킹
        existing = self._state.files.get(path)
        old_version = existing.version if existing else VersionVector.empty()
        new_version = old_version.update(self._state.device_id)
        self._state.update_file(
            path,
            FileEntry(
                mtime=existing.mtime if existing else 0.0,
                size=existing.size if existing else 0,
                drive_id=existing.drive_id if existing else None,
                version=new_version,
                deleted=True,
                deleted_at=time.time(),
            ),
        )
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

        if self._is_echo_drive(file_id):
            logger.debug(f"echo 억제 (drive_write): {file_id}")
            return None

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
            if should_ignore(existing_path):
                return None

            # md5 에코 가드 (v2 핫픽스): Drive가 통보한 변경의 md5가 state에
            # 저장된 md5와 동일하면 이는 우리가 방금 올린 파일의 Changes API
            # 지각 보고. 그대로 download하면 사용자가 방금 편집한 로컬본을
            # 덮어쓰게 되어 "내용이 이전으로 돌아감" 회귀 발생.
            remote_md5 = file_meta.get("md5Checksum") or file_meta.get("md5")
            state_entry = self._state.files.get(existing_path)
            if (
                state_entry is not None
                and state_entry.md5 is not None
                and remote_md5
                and state_entry.md5 == remote_md5
            ):
                logger.debug(
                    f"echo 억제 (remote md5 == state md5): {existing_path}"
                )
                return None

            # 이미 알고 있는 파일 → 재다운로드
            return {
                "type": ACTION_DOWNLOAD,
                "file_id": file_id,
                "path": existing_path,
                "reason": "remote_modified",
            }

        if should_ignore(name):
            return None

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
