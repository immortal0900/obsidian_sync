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

import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

import yaml

from core.drive_sync import DriveSync
from core.watcher import VaultWatcher


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


def main() -> None:
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
