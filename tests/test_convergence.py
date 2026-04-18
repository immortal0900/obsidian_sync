"""Tests for ConvergenceManager (Sprint 4 P0-2)."""
from __future__ import annotations

import time
from typing import Any

from src.convergence import ConvergenceManager, ConvergenceState

# ── Helpers ─────────────────────────────────────────────────────────


class InMemoryStore:
    """Simulates Drive convergence.json with etag optimistic concurrency."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.etag: str = "0"
        self._counter = 0

    def read(self) -> tuple[dict[str, Any], str]:
        return self.data, self.etag

    def write(self, data: dict[str, Any], etag: str) -> bool:
        if etag != self.etag:
            return False  # Conflict
        self._counter += 1
        self.etag = str(self._counter)
        self.data = data
        return True


def make_manager(store: InMemoryStore | None = None) -> ConvergenceManager:
    if store is None:
        store = InMemoryStore()
    mgr = ConvergenceManager(read_fn=store.read, write_fn=store.write)
    mgr._sleep = lambda s: None  # Skip sleeps in tests
    return mgr


# ── Single device convergence ──────────────────────────────────────


def test_single_device_immediate_convergence() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    ok = mgr.report_seen("device_a", ["tomb1", "tomb2"])
    assert ok is True

    assert mgr.check_converged("tomb1") is True
    assert mgr.check_converged("tomb2") is True


def test_no_devices_trivially_converged() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)
    # No devices reported at all
    assert mgr.check_converged("tomb1") is True


# ── Two-device convergence ─────────────────────────────────────────


def test_two_devices_both_must_confirm() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    mgr.report_seen("device_a", ["tomb1"])
    mgr.report_seen("device_b", [])  # device_b is active but hasn't confirmed

    assert mgr.check_converged("tomb1") is False

    mgr.report_seen("device_b", ["tomb1"])
    assert mgr.check_converged("tomb1") is True


def test_two_devices_partial_tombstone_set() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    mgr.report_seen("device_a", ["tomb1", "tomb2"])
    mgr.report_seen("device_b", ["tomb1"])

    assert mgr.check_converged("tomb1") is True
    assert mgr.check_converged("tomb2") is False


# ── Blacklist ──────────────────────────────────────────────────────


def test_blacklisted_device_excluded_from_convergence() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    mgr.report_seen("device_a", ["tomb1"])
    mgr.report_seen("device_b", [])  # active but no confirmation

    assert mgr.check_converged("tomb1") is False

    mgr.blacklist_device("device_b")
    assert mgr.check_converged("tomb1") is True


def test_blacklist_idempotent() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    mgr.blacklist_device("dead_device")
    mgr.blacklist_device("dead_device")

    state = mgr.get_state()
    assert state.blacklist.count("dead_device") == 1


# ── gc_eligible ────────────────────────────────────────────────────


def test_gc_eligible_converged_and_expired() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    deleted_at = time.time() - 91 * 86400  # 91 days ago
    mgr.report_seen("device_a", ["tomb1"])

    assert mgr.gc_eligible("tomb1", deleted_at) is True


def test_gc_not_eligible_before_90_days() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    deleted_at = time.time() - 30 * 86400  # 30 days ago
    mgr.report_seen("device_a", ["tomb1"])

    assert mgr.gc_eligible("tomb1", deleted_at) is False


def test_gc_not_eligible_not_converged() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    deleted_at = time.time() - 91 * 86400
    mgr.report_seen("device_a", ["tomb1"])
    mgr.report_seen("device_b", [])  # not confirmed

    assert mgr.gc_eligible("tomb1", deleted_at) is False


def test_gc_eligible_with_blacklist() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    deleted_at = time.time() - 91 * 86400
    mgr.report_seen("device_a", ["tomb1"])
    mgr.report_seen("device_b", [])
    mgr.blacklist_device("device_b")

    assert mgr.gc_eligible("tomb1", deleted_at) is True


def test_gc_eligible_explicit_now() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    deleted_at = 1000000.0
    now = deleted_at + 90 * 86400 - 1  # just before 90 days
    mgr.report_seen("device_a", ["tomb1"])

    assert mgr.gc_eligible("tomb1", deleted_at, now=now) is False

    now = deleted_at + 90 * 86400 + 1  # just after 90 days
    assert mgr.gc_eligible("tomb1", deleted_at, now=now) is True


# ── Retry on conflict ──────────────────────────────────────────────


def test_retry_on_etag_conflict() -> None:
    """Simulate etag conflict on first write, success on retry."""
    store = InMemoryStore()
    original_write = store.write
    call_count = 0

    def flaky_write(data: dict, etag: str) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate another device writing first
            store.etag = "conflict"
            return False
        # Fix the etag for retry
        return original_write(data, etag)

    mgr = ConvergenceManager(read_fn=store.read, write_fn=flaky_write)
    mgr._sleep = lambda s: None

    # Should succeed after retry
    ok = mgr.report_seen("device_a", ["tomb1"])
    assert ok is True
    assert call_count >= 2


def test_retry_exhausted() -> None:
    """All retries fail → returns False."""
    store = InMemoryStore()
    mgr = ConvergenceManager(
        read_fn=store.read,
        write_fn=lambda data, etag: False,
    )
    mgr._sleep = lambda s: None

    ok = mgr.report_seen("device_a", ["tomb1"])
    assert ok is False


# ── ConvergenceState serialization ─────────────────────────────────


def test_state_roundtrip() -> None:
    state = ConvergenceState(
        schema="v1",
        devices={"dev1": {"last_seen": 1.0, "confirmed_tombstones": ["t1"]}},
        blacklist=["dead1"],
    )
    d = state.to_dict()
    restored = ConvergenceState.from_dict(d)
    assert restored.schema == "v1"
    assert "dev1" in restored.devices
    assert restored.blacklist == ["dead1"]


def test_state_from_empty_dict() -> None:
    state = ConvergenceState.from_dict({})
    assert state.schema == "v1"
    assert state.devices == {}
    assert state.blacklist == []


# ── report_seen accumulates ────────────────────────────────────────


def test_report_seen_accumulates_tombstones() -> None:
    store = InMemoryStore()
    mgr = make_manager(store)

    mgr.report_seen("dev_a", ["t1", "t2"])
    mgr.report_seen("dev_a", ["t3"])

    state = mgr.get_state()
    confirmed = state.devices["dev_a"]["confirmed_tombstones"]
    assert set(confirmed) == {"t1", "t2", "t3"}
