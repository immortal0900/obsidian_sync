"""Write-Ahead Log for sync actions (spec §PR4).

Records action intent before execution, marks resolved on success.
On boot, replays unresolved intents to recover from partial failures
(e.g., SIGKILL between Drive delete and state save).

Storage format: JSONL (one JSON object per line), append-only.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class IntentLog:
    """Append-only WAL for sync engine actions."""

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, action: dict[str, Any]) -> str:
        """Record an intent before executing the action.

        Returns the generated intent_id (UUID).
        """
        intent_id = uuid.uuid4().hex
        entry = {
            "id": intent_id,
            "action": action,
            "ts": time.time(),
        }
        self._append(entry)
        logger.debug(f"Intent recorded: {intent_id} -> {action.get('type')}")
        return intent_id

    def resolve(self, intent_id: str) -> None:
        """Mark an intent as successfully resolved."""
        entry = {
            "id": intent_id,
            "resolved": True,
            "ts": time.time(),
        }
        self._append(entry)
        logger.debug(f"Intent resolved: {intent_id}")

    def replay(self, execute_fn: Any) -> int:
        """Replay unresolved intents on boot.

        Args:
            execute_fn: callable that takes an action dict and executes it.

        Returns:
            Number of intents replayed.
        """
        unresolved = self._get_unresolved()
        if not unresolved:
            return 0

        logger.info(f"Intent replay: {len(unresolved)} unresolved intent(s)")
        replayed = 0

        for intent_id, action in unresolved.items():
            try:
                logger.info(
                    f"Replaying intent {intent_id}: {action.get('type')}"
                )
                execute_fn(action)
                self.resolve(intent_id)
                replayed += 1
            except Exception:
                logger.warning(
                    f"Intent replay failed: {intent_id} -> {action.get('type')}. "
                    "Will be retried on next sync cycle.",
                    exc_info=True,
                )

        return replayed

    def compact(self) -> int:
        """Remove resolved entries, keeping only unresolved ones.

        Returns:
            Number of entries removed.
        """
        unresolved = self._get_unresolved()
        original_count = self._count_lines()

        if not unresolved:
            # All resolved — truncate file
            if self._path.exists():
                self._path.write_text("", encoding="utf-8")
            return original_count

        # Rewrite with only unresolved record entries
        lines: list[str] = []
        for intent_id, action in unresolved.items():
            entry = {"id": intent_id, "action": action, "ts": time.time()}
            lines.append(json.dumps(entry, ensure_ascii=False))

        self._path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return original_count - len(lines)

    def _get_unresolved(self) -> dict[str, dict[str, Any]]:
        """Parse log and return {intent_id: action} for unresolved intents."""
        if not self._path.exists():
            return {}

        recorded: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()

        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(f"Corrupt intent log line: {line!r}")
                        continue

                    intent_id = entry.get("id", "")
                    if entry.get("resolved"):
                        resolved.add(intent_id)
                    elif "action" in entry:
                        recorded[intent_id] = entry["action"]
        except OSError:
            logger.warning("Failed to read intent log", exc_info=True)
            return {}

        return {k: v for k, v in recorded.items() if k not in resolved}

    def _count_lines(self) -> int:
        """Count non-empty lines in the log file."""
        if not self._path.exists():
            return 0
        try:
            with open(self._path, encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _append(self, entry: dict[str, Any]) -> None:
        """Append a single JSON line. Uses os.fsync for durability."""
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        )
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
