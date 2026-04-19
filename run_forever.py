"""Auto-restart wrapper for obsidian_sync.

Launches `uv run python -m src.main --config config.yaml` and restarts it
on any exit (crash, segfault, network-induced abort). Intent Log WAL in
the main app recovers unfinished actions on each restart.

Usage
-----
    uv run python run_forever.py                    # default config.yaml
    uv run python run_forever.py config_blog.yaml   # custom config

Graceful shutdown: Ctrl+C propagates to the child and exits cleanly.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from datetime import datetime

DEFAULT_CONFIG = "config.yaml"
RESTART_DELAY_SECONDS = 5
WRAPPER_TAG = "[run_forever]"

_shutdown_requested = False


def _handle_sigint(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    print(f"{WRAPPER_TAG} shutdown signal received")


def _log(msg: str) -> None:
    print(f"{WRAPPER_TAG} {datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def main() -> int:
    config = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (AttributeError, ValueError):
        pass  # Windows may not support SIGTERM in all contexts

    restarts = 0
    while not _shutdown_requested:
        _log(f"starting obsidian_sync (run #{restarts + 1}, config={config})")

        try:
            proc = subprocess.Popen(
                ["uv", "run", "python", "-m", "src.main", "--config", config]
            )
        except FileNotFoundError:
            _log("uv command not found in PATH — aborting")
            return 1

        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            _log("KeyboardInterrupt — terminating child")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _log("child did not exit in 10s — killing")
                proc.kill()
            return 0

        if _shutdown_requested:
            _log("shutdown requested — not restarting")
            return 0

        restarts += 1
        _log(
            f"child exited (code={rc}); restarting in {RESTART_DELAY_SECONDS}s "
            f"(total restarts={restarts})"
        )
        time.sleep(RESTART_DELAY_SECONDS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
