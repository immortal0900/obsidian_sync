"""양쪽 동시 수정 시 충돌 사본 생성 전략.

전략: 원본 파일은 클라우드 버전으로 덮어쓰고,
로컬 버전은 `{stem}.conflict-{device_id}-{YYYYMMDD-HHMMSS}.{ext}` 형식의
충돌 사본으로 보존한다. 자동 병합이나 최신 우선 전략을 쓰지 않는다.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 반환값 상수
CONFLICT_CREATED = "conflict_created"
AUTO_RESOLVED = "auto_resolved"


class ConflictResolver:
    """충돌 발생 시 양쪽 보존 전략으로 처리한다.

    - 로컬 버전을 별도 파일명으로 복사한다.
    - 원본 경로에는 호출자(sync_engine)가 이후 다운로드로 덮어쓴다.
    - 충돌 사본의 이름이 중복되지 않도록 1초 간격 내에서도 유일성을 보장한다.
    """

    def __init__(self, device_id: str, vault_path: Path) -> None:
        self._device_id = device_id
        self._vault_path = vault_path

    def resolve(
        self,
        path: str,
        local_info: dict,
        remote_info: dict,
    ) -> str:
        """충돌을 처리한다.

        인자:
            path: 볼트 기준 상대 경로 (POSIX).
            local_info: 로컬 쪽 메타데이터 (mtime, size 등).
            remote_info: 클라우드 쪽 메타데이터 (file_id, modified_time 등).

        반환값: "conflict_created" (사본 생성) 또는 "auto_resolved" (사본 불필요).
        """
        local_abs = self._vault_path / path
        if not local_abs.exists():
            # 로컬 파일이 이미 사라짐 → 보존할 내용이 없음
            logger.warning(f"충돌 대상 로컬 파일이 없습니다: {path}")
            return AUTO_RESOLVED

        conflict_path = self._build_conflict_path(path)
        conflict_abs = self._vault_path / conflict_path

        conflict_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_abs), str(conflict_abs))

        logger.info(
            f"충돌 사본 생성: {path} → {conflict_path} "
            f"(local_info keys={sorted(local_info)}, remote_info keys={sorted(remote_info)})"
        )
        return CONFLICT_CREATED

    def _build_conflict_path(self, path: str) -> str:
        """충돌 사본의 상대 경로를 만든다.

        규칙:
            {stem}.conflict-{device_id}-{YYYYMMDD-HHMMSS}.{ext}
        확장자가 없으면 뒤에 `.conflict-...`를 부착한다.
        이미 존재하는 이름과 충돌하면 초 단위로 증가시켜 유일성을 보장한다.
        """
        p = Path(path)
        parent = p.parent
        stem = p.stem
        ext = p.suffix  # includes dot, or "" if none

        now = time.time()

        # 초 단위 증가로 중복 방지
        attempts = 0
        while True:
            ts = datetime.fromtimestamp(now + attempts).strftime("%Y%m%d-%H%M%S")
            conflict_name = f"{stem}.conflict-{self._device_id}-{ts}{ext}"
            candidate = (parent / conflict_name).as_posix() if str(parent) != "." else conflict_name

            candidate_abs = self._vault_path / candidate
            if not candidate_abs.exists():
                return candidate

            attempts += 1
            if attempts > 60:
                # 안전장치: 1분 내 중복이 60회 발생하면 마이크로초 suffix 부착
                micro_ts = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S-%f")
                fallback_name = f"{stem}.conflict-{self._device_id}-{micro_ts}{ext}"
                fallback_rel = (
                    (parent / fallback_name).as_posix()
                    if str(parent) != "."
                    else fallback_name
                )
                logger.warning(f"충돌 사본 이름 중복이 과다하여 micro-suffix 사용: {fallback_rel}")
                return fallback_rel
