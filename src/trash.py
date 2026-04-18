"""로컬 삭제 파일을 .sync/trash/에 보관하는 TrashManager.

삭제된 파일을 flat UUID 경로로 이동하여 Windows MAX_PATH 제한을 회피하고,
메타데이터 JSON으로 원본 정보를 보존한다. retention 경과 후 GC로 정리한다.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS: int = 30


@dataclass
class TrashEntry:
    """trash에 보관된 파일 하나의 메타데이터."""

    uuid: str
    original_path: str
    mtime: float
    size: int
    deleted_at: float
    md5: str | None = None


class TrashManager:
    """로컬 삭제 파일을 .sync/trash/에 보관·관리한다.

    구조:
        .sync/trash/{uuid}       — 삭제된 파일 본체
        .sync/trash/{uuid}.json  — 메타데이터 (원본 경로, mtime, md5 등)
    """

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._trash_dir = vault_path / ".sync" / "trash"

    @property
    def trash_dir(self) -> Path:
        """trash 디렉토리 경로."""
        return self._trash_dir

    def _ensure_dir(self) -> None:
        """trash 디렉토리가 없으면 생성한다."""
        self._trash_dir.mkdir(parents=True, exist_ok=True)

    def move(
        self,
        abs_path: Path,
        rel_path: str,
        md5: str | None = None,
    ) -> str:
        """파일을 trash로 이동한다.

        Args:
            abs_path: 삭제할 파일의 절대 경로.
            rel_path: 볼트 기준 상대 경로 (메타데이터 기록용).
            md5: 파일의 md5 해시 (있으면 기록).

        Returns:
            생성된 trash entry의 UUID.

        Raises:
            FileNotFoundError: abs_path 파일이 존재하지 않을 때.
        """
        if not abs_path.exists():
            raise FileNotFoundError(f"삭제 대상 파일 없음: {abs_path}")

        self._ensure_dir()

        entry_id = str(uuid.uuid4())
        trash_file = self._trash_dir / entry_id
        meta_file = self._trash_dir / f"{entry_id}.json"

        # 파일 stat 수집 (이동 전)
        stat = abs_path.stat()
        now = time.time()

        # 파일 이동
        shutil.move(str(abs_path), str(trash_file))

        # 메타데이터 기록
        meta: dict[str, Any] = {
            "original_path": rel_path,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "deleted_at": now,
        }
        if md5 is not None:
            meta["md5"] = md5

        meta_file.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info(f"파일을 trash로 이동: {rel_path} → {entry_id}")
        return entry_id

    def gc(
        self,
        now: float | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> int:
        """retention 경과 항목을 삭제한다.

        Args:
            now: 현재 시각(초). None이면 time.time() 사용.
            retention_days: 보존 기간(일).

        Returns:
            삭제된 항목 수.
        """
        if now is None:
            now = time.time()

        if not self._trash_dir.exists():
            return 0

        cutoff = now - (retention_days * 86400)
        removed = 0

        for meta_file in self._trash_dir.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                deleted_at = meta.get("deleted_at", 0)
                if deleted_at < cutoff:
                    # 본체 삭제
                    entry_id = meta_file.stem
                    body_file = self._trash_dir / entry_id
                    if body_file.exists():
                        body_file.unlink()
                    meta_file.unlink()
                    removed += 1
                    logger.debug(f"trash GC: {entry_id} 삭제")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"trash GC 중 오류: {meta_file}: {e}")

        if removed > 0:
            logger.info(f"trash GC 완료: {removed}개 항목 삭제")
        return removed

    def list_entries(self) -> list[TrashEntry]:
        """현재 trash 내 모든 항목을 반환한다."""
        entries: list[TrashEntry] = []
        if not self._trash_dir.exists():
            return entries

        for meta_file in sorted(self._trash_dir.glob("*.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                entries.append(
                    TrashEntry(
                        uuid=meta_file.stem,
                        original_path=meta["original_path"],
                        mtime=meta["mtime"],
                        size=meta["size"],
                        deleted_at=meta["deleted_at"],
                        md5=meta.get("md5"),
                    )
                )
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning(f"trash 항목 읽기 실패: {meta_file}: {e}")

        return entries

    def restore(self, entry_uuid: str, target_path: Path) -> None:
        """trash에서 파일을 복원한다.

        Args:
            entry_uuid: 복원할 항목의 UUID.
            target_path: 복원 대상 절대 경로.

        Raises:
            FileNotFoundError: 해당 UUID의 파일이 trash에 없을 때.
        """
        body_file = self._trash_dir / entry_uuid
        meta_file = self._trash_dir / f"{entry_uuid}.json"

        if not body_file.exists():
            raise FileNotFoundError(f"trash에 해당 파일 없음: {entry_uuid}")

        # 대상 디렉토리 보장
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(body_file), str(target_path))

        # 메타 삭제
        if meta_file.exists():
            meta_file.unlink()

        logger.info(f"trash에서 복원: {entry_uuid} → {target_path}")
