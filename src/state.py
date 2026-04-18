"""sync_state.json 관리 (읽기/쓰기/스캔/비교).

볼트의 동기화 상태를 JSON 파일로 관리한다.
파일별 mtime, size, drive_id, version vector를 추적하고,
두 시점 간의 차이를 계산하는 기능을 제공한다.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import (
    STATE_SAVE_DEBOUNCE_SECONDS,
    SyncConfig,
    should_ignore,
)
from src.version_vector import VersionVector

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """파일 하나의 동기화 상태."""

    mtime: float
    size: int
    drive_id: str | None = None
    version: VersionVector = field(default_factory=VersionVector.empty)
    deleted: bool = False
    deleted_at: float | None = None
    md5: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화용 딕셔너리로 변환한다."""
        d: dict[str, Any] = {"mtime": self.mtime, "size": self.size}
        if self.drive_id is not None:
            d["drive_id"] = self.drive_id
        if self.version.counters:
            d["version"] = self.version.to_dict()
        if self.deleted:
            d["deleted"] = True
        if self.deleted_at is not None:
            d["deleted_at"] = self.deleted_at
        if self.md5 is not None:
            d["md5"] = self.md5
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEntry:
        """딕셔너리에서 FileEntry를 생성한다.

        v1 데이터(version/deleted 없음)도 호환 처리한다.
        """
        return cls(
            mtime=float(data["mtime"]),
            size=int(data["size"]),
            drive_id=data.get("drive_id"),
            version=VersionVector.from_dict(data.get("version")),
            deleted=bool(data.get("deleted", False)),
            deleted_at=data.get("deleted_at"),
            md5=data.get("md5"),
        )


@dataclass
class DiffResult:
    """두 파일 목록 비교 결과."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class SyncState:
    """볼트의 동기화 상태 파일(sync_state.json)을 관리한다.

    상태 파일 형식:
    {
        "version": 1,
        "device_id": "my_pc",
        "page_token": "67890",
        "last_synced_at": 1713100000.0,
        "files": {
            "daily/2026-04-14.md": {"mtime": ..., "size": ..., "drive_id": "..."}
        }
    }
    """

    VERSION = 2

    def __init__(self, config: SyncConfig) -> None:
        self._config = config
        self._state_dir = config.state_dir
        self._state_file = config.state_file
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._dirty = False

        # 인메모리 상태
        self.device_id: str = config.device_id
        self.page_token: str | None = None
        self.last_synced_at: float | None = None
        self.files: dict[str, FileEntry] = {}
        self.known_device_ids: set[str] = set()  # 충돌 감지용 전체 device_id 목록

    @property
    def vault_path(self) -> Path:
        """볼트 루트 절대 경로."""
        return self._config.vault_path

    def load(self) -> bool:
        """sync_state.json을 읽어서 인메모리 상태를 채운다.

        파일이 없으면 False를 반환한다.
        JSON 파싱 실패 시 .backup으로 이름변경 후 False를 반환한다.
        """
        if not self._state_file.exists():
            logger.info("상태 파일이 없습니다: %s", self._state_file)
            return False

        try:
            raw_text = self._state_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"상태 파일 파싱 실패: {e}")
            self._backup_corrupt_state()
            return False

        # 스키마 버전 검증 + v1→v2 자동 마이그레이션
        file_version = data.get("version")
        if file_version == 1:
            logger.info("v1 → v2 자동 마이그레이션 시작")
            self._backup_v1_state()
            data["version"] = 2
            # v1 entries에는 version/deleted 필드가 없으므로
            # from_dict에서 자동으로 empty/False 기본값 적용
        elif file_version != self.VERSION:
            logger.warning(
                f"상태 파일 version 불일치: file={file_version}, "
                f"expected={self.VERSION} → 전체 재동기화 모드로 전환"
            )
            self._backup_corrupt_state()
            return False

        # 인메모리 상태 채우기
        with self._lock:
            self.device_id = data.get("device_id", self._config.device_id)
            self.page_token = data.get("page_token")
            self.last_synced_at = data.get("last_synced_at")
            self.files = {}
            for rel_path, file_data in data.get("files", {}).items():
                self.files[rel_path] = FileEntry.from_dict(file_data)
            self.known_device_ids = set(data.get("known_device_ids", []))

        # v1에서 마이그레이션된 경우 즉시 v2로 저장
        if file_version == 1:
            self.save(immediate=True)
            logger.info(
                f"v1 → v2 마이그레이션 완료: 파일 {len(self.files)}개"
            )

        # device_id prefix(8자) 충돌 감지
        self._check_device_prefix_collision()

        logger.info(
            f"상태 파일 로드 완료: 파일 {len(self.files)}개, "
            f"page_token={self.page_token}"
        )
        return True

    def _backup_corrupt_state(self) -> None:
        """손상/버전 불일치 상태 파일을 .backup으로 이동한다."""
        backup_path = self._state_file.with_suffix(".json.backup")
        try:
            self._state_file.rename(backup_path)
            logger.info(f"상태 파일을 백업했습니다: {backup_path}")
        except OSError:
            logger.exception("상태 파일 백업 실패")

    def _check_device_prefix_collision(self) -> None:
        """known_device_ids 중 다른 device가 동일 8자 prefix를 갖는지 검사.

        VV counters에는 prefix(8자)만 저장되므로, 충돌 감지를 위해
        전체 device_id를 known_device_ids에 별도 추적한다.
        PR2에서 원격 vector 수신 시 known_device_ids가 채워진다.
        """
        my_prefix = self.device_id[:8]
        for other_id in self.known_device_ids:
            if other_id == self.device_id:
                continue
            if other_id[:8] == my_prefix:
                logger.warning(
                    f"device_id prefix 충돌 감지: "
                    f"내 device={self.device_id}, "
                    f"다른 device={other_id}, "
                    f"공유 prefix={my_prefix}"
                )
                return  # 한 번만 경고

    def _backup_v1_state(self) -> None:
        """v1→v2 마이그레이션 전 v1 백업을 생성한다 (다운그레이드 안전망)."""
        backup_path = self._state_file.with_name("sync_state.json.v1.bak")
        try:
            shutil.copy2(str(self._state_file), str(backup_path))
            logger.info(f"v1 상태 파일 백업 완료: {backup_path}")
        except OSError:
            logger.exception("v1 상태 파일 백업 실패")

    def save(self, immediate: bool = False) -> None:
        """현재 상태를 sync_state.json에 저장한다.

        immediate=False이면 5초 디바운스 타이머를 사용하고,
        immediate=True이면 즉시 저장한다.
        """
        if immediate:
            # 보류 중인 타이머 취소
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            self._write_state_file()
            return

        # 디바운스: 기존 타이머 리셋
        with self._lock:
            self._dirty = True
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(
                STATE_SAVE_DEBOUNCE_SECONDS, self._write_state_file
            )
            self._save_timer.daemon = True
            self._save_timer.start()

    def _write_state_file(self) -> None:
        """실제 파일 쓰기를 수행한다. .sync/ 디렉토리를 자동 생성한다."""
        self._state_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            data = {
                "version": self.VERSION,
                "device_id": self.device_id,
                "page_token": self.page_token,
                "last_synced_at": self.last_synced_at,
                "known_device_ids": sorted(self.known_device_ids),
                "files": {
                    path: entry.to_dict()
                    for path, entry in self.files.items()
                },
            }
            self._dirty = False
            self._save_timer = None

        # 원자적 쓰기: 임시 파일에 쓰고 rename
        json_text = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_dir), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json_text)
                os.replace(tmp_path, str(self._state_file))
            except BaseException:
                # 임시 파일 정리
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.debug(f"상태 파일 저장 완료: 파일 {len(data['files'])}개")
        except Exception:
            logger.exception("상태 파일 저장 실패")

    def scan_local_files(self) -> dict[str, FileEntry]:
        """볼트 폴더를 스캔해서 {상대경로: FileEntry} 딕셔너리를 반환한다.

        IGNORE_PATTERNS에 해당하는 파일은 건너뛴다.
        기존 self.files에서 drive_id를 복사한다 (있는 경우).

        성능 참고: 파일 10,000개 기준 os.scandir 재귀 약 100-300ms.
        """
        result: dict[str, FileEntry] = {}
        vault_root = self._config.vault_path

        def _scan_dir(dir_path: Path) -> None:
            try:
                with os.scandir(dir_path) as entries:
                    for entry in entries:
                        entry_path = Path(entry.path)
                        rel_path = entry_path.relative_to(vault_root).as_posix()

                        if should_ignore(rel_path):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            _scan_dir(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                stat = entry.stat()
                                # 기존 files에서 drive_id, version 복사
                                existing = self.files.get(rel_path)
                                drive_id = existing.drive_id if existing else None
                                version = (
                                    existing.version
                                    if existing
                                    else VersionVector.empty()
                                )

                                result[rel_path] = FileEntry(
                                    mtime=stat.st_mtime,
                                    size=stat.st_size,
                                    drive_id=drive_id,
                                    version=version,
                                )
                            except OSError:
                                logger.warning(f"파일 stat 실패: {entry_path}")
            except PermissionError:
                logger.warning(f"디렉토리 접근 권한 없음: {dir_path}")

        _scan_dir(vault_root)
        logger.debug(f"로컬 스캔 완료: 파일 {len(result)}개")
        return result

    @staticmethod
    def diff(
        old_files: dict[str, FileEntry],
        new_files: dict[str, FileEntry],
    ) -> DiffResult:
        """두 파일 목록을 비교해서 추가/수정/삭제를 분류한다.

        수정 판정 기준: mtime 또는 size가 다른 경우.
        """
        old_keys = set(old_files.keys())
        new_keys = set(new_files.keys())

        added = sorted(new_keys - old_keys)
        deleted = sorted(old_keys - new_keys)

        modified: list[str] = []
        for key in sorted(old_keys & new_keys):
            old_entry = old_files[key]
            new_entry = new_files[key]
            if old_entry.mtime != new_entry.mtime or old_entry.size != new_entry.size:
                modified.append(key)

        return DiffResult(added=added, modified=modified, deleted=deleted)

    def update_file(self, rel_path: str, entry: FileEntry) -> None:
        """파일 항목 하나를 갱신하고 디바운스 저장을 예약한다."""
        with self._lock:
            self.files[rel_path] = entry
        self.save(immediate=False)

    def remove_file(self, rel_path: str) -> None:
        """파일 항목 하나를 삭제하고 디바운스 저장을 예약한다."""
        with self._lock:
            self.files.pop(rel_path, None)
        self.save(immediate=False)

    def shutdown(self) -> None:
        """프로그램 종료 시 보류 중인 타이머를 취소하고 즉시 저장한다."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None

        self.last_synced_at = time.time()
        self._write_state_file()
        logger.info("종료 시 상태 파일 저장 완료")
