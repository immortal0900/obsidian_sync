"""설정값 정의 및 로드.

YAML 설정 파일을 읽어 SyncConfig 데이터클래스로 변환하고,
동기화 제외 패턴과 적응형 폴링 상수를 제공한다.
"""
from __future__ import annotations

import fnmatch
import logging
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── 상태 파일 ─────────────────────────────────────────────────────────────

SYNC_STATE_DIR: str = ".sync"
STATE_FILE_NAME: str = "sync_state.json"

# ── 동기화 제외 패턴 ──────────────────────────────────────────────────────
# 1차 구현: .obsidian/ 전체 제외
# 향후: .obsidian/plugins/*/data.json 등 선택적 동기화 옵션 추가 예정

IGNORE_PATTERNS: list[str] = [
    ".obsidian/",          # Obsidian 설정 전체 (workspace.json 포함)
    ".sync/",              # 이 프로그램의 상태 파일
    ".trash/",             # Obsidian 휴지통
    ".smart-env/",         # Smart Connections 플러그인 폴더
    ".smart-connections/", # Smart Connections 구버전
    ".makemd/",            # MakeMD 플러그인
    ".git/",               # Git 메타데이터
    "undefined/",          # Smart Connections가 경로 오류로 생성하는 유령 폴더
    ".DS_Store",           # macOS Finder
    "Thumbs.db",           # Windows Explorer
    "*.ajson",             # Smart Connections 임베딩 캐시 (루트/중첩 포함)
    "*.smtcmp_*",          # Smart Connections 압축 캐시
    ".smtcmp_*",           # Smart Connections 숨김 캐시
    "*.tmp",               # 임시 파일
    "*.swp",               # Vim swap
    # 볼트 루트에 풀려 나온 Obsidian 설정 파일들 (.obsidian/ 바깥에 복사된 상태)
    "app.json",
    "appearance.json",
    "community-plugins.json",
    "core-plugins.json",
    "workspace.json",
    "data.json",
    "types.json",
    "smart_env.json",
    "supercharged-links-gen.css",
]

# ── 적응형 폴링 상수 ─────────────────────────────────────────────────────

POLL_MIN_INTERVAL: int = 10       # 활발할 때 최소 10초
POLL_MAX_INTERVAL: int = 120      # 조용할 때 최대 2분
POLL_START_INTERVAL: int = 30     # 시작 간격 30초
POLL_BACKOFF_FACTOR: float = 1.5  # 변경 없으면 간격 1.5배

# ── 상태 저장 디바운스 ────────────────────────────────────────────────────

STATE_SAVE_DEBOUNCE_SECONDS: float = 5.0

# ── 상태 파일 스키마 버전 ────────────────────────────────────────────────
STATE_VERSION: int = 2

# ── 로컬 Trash 보존 기간 ────────────────────────────────────────────────
DEFAULT_TRASH_RETENTION_DAYS: int = 30


@dataclass
class SyncConfig:
    """동기화 설정을 담는 데이터클래스."""

    vault_path: Path
    drive_folder_id: str
    device_id: str
    credentials_file: Path
    token_file: Path
    debounce_seconds: float = 2.0
    delete_local: bool = False
    trash_retention_days: int = DEFAULT_TRASH_RETENTION_DAYS
    hash_max_file_size_mb: int = 100
    hash_verification: bool = True
    log_level: str = "INFO"
    log_file: str = "obsidian_sync.log"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> SyncConfig:
        """YAML 파일에서 설정을 로드한다.

        기존 config.yaml 포맷을 그대로 읽으며,
        device_id 키가 없으면 hostname을 사용한다.
        """
        config_path = Path(path)
        if not config_path.exists():
            logger.error(f"설정 파일을 찾을 수 없습니다: {path}")
            sys.exit(1)

        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # watch_paths[0].path → vault_path
        watch_paths = raw.get("watch_paths", [])
        if not watch_paths:
            logger.error("config.yaml에 watch_paths가 비어 있습니다")
            sys.exit(1)

        vault_path = Path(watch_paths[0]["path"]).expanduser().resolve()

        # drive 섹션
        drive_cfg = raw.get("drive", {})
        drive_folder_id = drive_cfg.get("folder_id", "")
        if not drive_folder_id:
            logger.error("config.yaml에 drive.folder_id가 비어 있습니다")
            sys.exit(1)

        credentials_file = Path(drive_cfg.get("credentials_file", "credentials.json"))
        token_file = Path(drive_cfg.get("token_file", "token.json"))

        # device_id — 새 키, 없으면 hostname
        device_id = raw.get("device_id", socket.gethostname())

        # sync 섹션
        sync_cfg = raw.get("sync", {})
        debounce_seconds = float(sync_cfg.get("debounce_seconds", 2.0))
        delete_local = bool(sync_cfg.get("delete_local", False))
        trash_retention_days = int(
            sync_cfg.get("trash_retention_days", DEFAULT_TRASH_RETENTION_DAYS)
        )

        # logging 섹션
        log_cfg = raw.get("logging", {})
        log_level = log_cfg.get("level", "INFO")
        log_file = log_cfg.get("file", "obsidian_sync.log")
        log_max_bytes = int(log_cfg.get("max_bytes", 5 * 1024 * 1024))
        log_backup_count = int(log_cfg.get("backup_count", 3))

        # 검증
        if not vault_path.exists():
            logger.error(f"볼트 경로가 존재하지 않습니다: {vault_path}")
            sys.exit(1)

        if not credentials_file.exists():
            logger.error(
                f"인증 파일을 찾을 수 없습니다: {credentials_file}. "
                "Google Cloud Console에서 다운로드하세요."
            )
            sys.exit(1)

        return cls(
            vault_path=vault_path,
            drive_folder_id=drive_folder_id,
            device_id=device_id,
            credentials_file=credentials_file,
            token_file=token_file,
            debounce_seconds=debounce_seconds,
            delete_local=delete_local,
            trash_retention_days=trash_retention_days,
            log_level=log_level,
            log_file=log_file,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )

    @property
    def state_dir(self) -> Path:
        """상태 파일 디렉토리 경로를 반환한다."""
        return self.vault_path / SYNC_STATE_DIR

    @property
    def state_file(self) -> Path:
        """상태 파일 전체 경로를 반환한다."""
        return self.state_dir / STATE_FILE_NAME


def load_config(path: str | Path = "config.yaml") -> SyncConfig:
    """설정 파일을 로드하는 편의 함수."""
    return SyncConfig.from_yaml(path)


def should_ignore(rel_path: str) -> bool:
    """주어진 상대 경로가 동기화 제외 대상인지 판정한다.

    판정 규칙:
    - "/" 끝 패턴 (예: ".obsidian/"): 경로 구성요소에 해당 디렉토리가 포함되면 제외
    - "*" 포함 패턴 (예: "*.tmp"): 파일명에 대해 fnmatch 매칭
    - 그 외 (예: ".DS_Store"): 파일명 또는 전체 경로와 정확히 일치
    """
    # POSIX 스타일로 정규화
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")
    filename = parts[-1] if parts else ""

    for pattern in IGNORE_PATTERNS:
        if pattern.endswith("/"):
            # 디렉토리 패턴: 경로 구성요소에 포함 여부 확인
            dir_name = pattern.rstrip("/")
            if dir_name in parts:
                return True
        elif "*" in pattern:
            # 글로브 패턴: 파일명 또는 경로 상의 어떤 구성요소와도 매칭
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
        else:
            # 정확한 매칭: 파일명 또는 전체 경로
            if filename == pattern or normalized == pattern:
                return True

    return False
