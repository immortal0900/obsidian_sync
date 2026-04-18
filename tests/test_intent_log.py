"""Tests for IntentLog WAL (Sprint 4 P0-1)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.intent_log import IntentLog


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / ".sync" / "intent_log.jsonl"


@pytest.fixture
def intent_log(log_path: Path) -> IntentLog:
    return IntentLog(log_path)


# ── record / resolve roundtrip ─────────────────────────────────────


def test_record_creates_file(intent_log: IntentLog, log_path: Path) -> None:
    action = {"type": "upload", "path": "test.md"}
    intent_id = intent_log.record(action)
    assert log_path.exists()
    assert len(intent_id) == 32  # hex UUID


def test_record_resolve_roundtrip(intent_log: IntentLog, log_path: Path) -> None:
    action = {"type": "upload", "path": "test.md"}
    intent_id = intent_log.record(action)
    intent_log.resolve(intent_id)

    # Both lines should be in the file
    lines = [ln for ln in log_path.read_text("utf-8").strip().split("\n") if ln]
    assert len(lines) == 2

    record_line = json.loads(lines[0])
    assert record_line["id"] == intent_id
    assert record_line["action"] == action

    resolve_line = json.loads(lines[1])
    assert resolve_line["id"] == intent_id
    assert resolve_line["resolved"] is True


def test_multiple_records(intent_log: IntentLog) -> None:
    ids = []
    for i in range(5):
        ids.append(intent_log.record({"type": "upload", "path": f"file{i}.md"}))
    assert len(set(ids)) == 5  # all unique


# ── replay ──────────────────────────────────────────────────────────


def test_replay_empty_log(intent_log: IntentLog) -> None:
    execute_fn = MagicMock()
    count = intent_log.replay(execute_fn)
    assert count == 0
    execute_fn.assert_not_called()


def test_replay_unresolved_intents(intent_log: IntentLog) -> None:
    """SIGKILL simulation: record without resolve → replay re-executes."""
    action1 = {"type": "delete_remote", "file_id": "abc", "path": "old.md"}
    action2 = {"type": "upload", "path": "new.md"}

    intent_log.record(action1)
    intent_log.record(action2)  # both unresolved

    execute_fn = MagicMock()
    count = intent_log.replay(execute_fn)
    assert count == 2
    assert execute_fn.call_count == 2


def test_replay_skips_resolved(intent_log: IntentLog) -> None:
    action1 = {"type": "upload", "path": "a.md"}
    action2 = {"type": "upload", "path": "b.md"}

    id1 = intent_log.record(action1)
    intent_log.resolve(id1)
    intent_log.record(action2)  # unresolved

    execute_fn = MagicMock()
    count = intent_log.replay(execute_fn)
    assert count == 1
    execute_fn.assert_called_once_with(action2)


def test_replay_failure_logs_warning(intent_log: IntentLog) -> None:
    """Replay failure should not crash, should log warning."""
    action = {"type": "upload", "path": "fail.md"}
    intent_log.record(action)

    execute_fn = MagicMock(side_effect=RuntimeError("network error"))
    count = intent_log.replay(execute_fn)
    assert count == 0  # failed replay doesn't count


def test_replay_partial_failure(intent_log: IntentLog) -> None:
    """First succeeds, second fails."""
    action1 = {"type": "upload", "path": "ok.md"}
    action2 = {"type": "upload", "path": "fail.md"}
    intent_log.record(action1)
    intent_log.record(action2)

    call_count = 0

    def execute_fn(action: dict) -> None:
        nonlocal call_count
        call_count += 1
        if action["path"] == "fail.md":
            raise RuntimeError("boom")

    count = intent_log.replay(execute_fn)
    assert count == 1  # only first succeeded
    assert call_count == 2  # both were attempted


# ── compact ─────────────────────────────────────────────────────────


def test_compact_removes_resolved(intent_log: IntentLog, log_path: Path) -> None:
    id1 = intent_log.record({"type": "upload", "path": "a.md"})
    intent_log.resolve(id1)
    id2 = intent_log.record({"type": "upload", "path": "b.md"})

    removed = intent_log.compact()
    assert removed >= 2  # at least record+resolve of id1

    # Only unresolved remains
    content = log_path.read_text("utf-8").strip()
    lines = [ln for ln in content.split("\n") if ln]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["id"] == id2


def test_compact_all_resolved(intent_log: IntentLog, log_path: Path) -> None:
    id1 = intent_log.record({"type": "upload", "path": "a.md"})
    intent_log.resolve(id1)

    removed = intent_log.compact()
    assert removed == 2  # record + resolve
    assert log_path.read_text("utf-8").strip() == ""


def test_compact_empty_log(intent_log: IntentLog) -> None:
    removed = intent_log.compact()
    assert removed == 0


# ── corrupt log handling ────────────────────────────────────────────


def test_corrupt_line_skipped(intent_log: IntentLog, log_path: Path) -> None:
    """Corrupt lines should be skipped without crash."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a corrupt line followed by a valid one
    action = {"type": "upload", "path": "test.md"}
    id1 = "a" * 32
    lines = [
        "THIS IS NOT JSON\n",
        json.dumps({"id": id1, "action": action, "ts": 1.0}) + "\n",
    ]
    log_path.write_text("".join(lines), encoding="utf-8")

    execute_fn = MagicMock()
    count = intent_log.replay(execute_fn)
    assert count == 1
    execute_fn.assert_called_once_with(action)


# ── SIGKILL simulation E2E ──────────────────────────────────────────


def test_sigkill_simulation(tmp_path: Path) -> None:
    """Simulate: record delete_remote → SIGKILL (no resolve) → new process → replay."""
    log_path = tmp_path / ".sync" / "intent_log.jsonl"

    # Process 1: record intent, then "crash" (no resolve)
    log1 = IntentLog(log_path)
    action = {"type": "delete_remote", "file_id": "xyz", "path": "notes/old.md"}
    log1.record(action)
    # SIGKILL here — no resolve()

    # Process 2: new IntentLog instance, replay
    log2 = IntentLog(log_path)
    execute_fn = MagicMock()
    count = log2.replay(execute_fn)
    assert count == 1
    execute_fn.assert_called_once_with(action)

    # After successful replay, intent should be resolved
    execute_fn2 = MagicMock()
    count2 = log2.replay(execute_fn2)
    assert count2 == 0  # already resolved


def test_file_size_decreases_after_compact(
    intent_log: IntentLog, log_path: Path
) -> None:
    """Compact should reduce file size."""
    for i in range(10):
        iid = intent_log.record({"type": "upload", "path": f"f{i}.md"})
        intent_log.resolve(iid)
    # Add one unresolved
    intent_log.record({"type": "upload", "path": "pending.md"})

    size_before = log_path.stat().st_size
    intent_log.compact()
    size_after = log_path.stat().st_size
    assert size_after < size_before
