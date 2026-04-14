"""
Obsidian ↔ Google Drive bidirectional sync daemon.

Lifecycle:
1. Load config.yaml
2. Set up rotating log handler
3. Authenticate to Google Drive (OAuth2)
4. Set up file watcher (but don't start yet)
5. Start Drive polling thread
6. Start watchdog observer
7. Sleep main thread, handling SIGTERM / KeyboardInterrupt
8. Graceful shutdown
"""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from core.drive_sync import DriveSync
from core.watcher import VaultWatcher

LOCK_FILE = Path(__file__).parent / "daemon.lock"


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(
            f"ERROR: '{path}' not found.\n"
            "Copy config.example.yaml to config.yaml and fill in your settings.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "obsidian_sync.log")
    max_bytes = log_cfg.get("max_bytes", 5 * 1024 * 1024)
    backup_count = log_cfg.get("backup_count", 3)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    ]

    # Also log to stderr when there is a real console (not pythonw)
    try:
        if sys.stderr and sys.stderr.fileno() >= 0:
            handlers.append(logging.StreamHandler(sys.stderr))
    except Exception:
        pass

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def _acquire_lock() -> None:
    """중복 실행 방지용 PID 락 파일."""
    if LOCK_FILE.exists():
        old_pid = LOCK_FILE.read_text().strip()
        # 이전 프로세스가 아직 살아있는지 확인
        try:
            os.kill(int(old_pid), 0)
            print(
                f"ERROR: 데몬이 이미 실행 중입니다 (PID {old_pid}). "
                "중복 실행을 방지합니다.",
                file=sys.stderr,
            )
            sys.exit(1)
        except (ProcessLookupError, ValueError, OSError):
            pass  # 이전 프로세스가 죽었으면 락 파일 덮어쓰기

    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))


def main() -> None:
    _acquire_lock()
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger(__name__)
    logger.info("=== Obsidian Sync Daemon starting ===")

    vault_root = (
        Path(config["watch_paths"][0]["path"]).expanduser().resolve()
    )
    logger.info("Vault root: %s", vault_root)

    drive_sync = DriveSync(config=config, vault_root=vault_root)
    drive_sync.authenticate()
    drive_sync.start_polling()

    watcher = VaultWatcher(config=config, drive_sync=drive_sync)
    watcher.setup()
    watcher.start()

    def _shutdown(signum=None, frame=None) -> None:
        logger.info("Shutdown signal received, stopping...")
        watcher.stop()
        drive_sync.stop_polling()
        logger.info("=== Obsidian Sync Daemon stopped ===")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Daemon running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
