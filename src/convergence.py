"""Convergence protocol for tombstone safe GC (spec §3.5, PR4).

All active devices must confirm they have seen a tombstone before it
can be hard-deleted. The convergence state is stored in a shared
Drive file `.sync/convergence.json`.

Concurrency: optimistic concurrency with Drive etag-based conditional
PATCH. On conflict, exponential backoff + jitter retry.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Retry constants for etag conflicts
INITIAL_BACKOFF_S = 0.5
BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_S = 8.0
MAX_RETRIES = 6


@dataclass
class ConvergenceState:
    """In-memory representation of convergence.json."""

    schema: str = "v1"
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    blacklist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "devices": self.devices,
            "blacklist": self.blacklist,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConvergenceState:
        return cls(
            schema=data.get("schema", "v1"),
            devices=data.get("devices", {}),
            blacklist=data.get("blacklist", []),
        )


class ConvergenceManager:
    """Manages tombstone convergence across devices.

    In production, `read_fn` and `write_fn` interact with the Drive API
    to read/write `.sync/convergence.json`. For testing, they can be
    replaced with in-memory functions.
    """

    def __init__(
        self,
        read_fn: Any = None,
        write_fn: Any = None,
    ) -> None:
        """Initialize with Drive read/write callbacks.

        Args:
            read_fn: () -> (dict, etag) — reads convergence.json from Drive.
            write_fn: (dict, etag) -> bool — writes convergence.json with
                      conditional etag. Returns True on success, False on conflict.
        """
        self._read_fn = read_fn
        self._write_fn = write_fn

    def report_seen(
        self, device_id: str, tombstone_ids: list[str]
    ) -> bool:
        """Report that this device has confirmed the given tombstones.

        Returns True on success, False if all retries exhausted.
        """
        return self._retry_update(
            lambda state: self._apply_report_seen(state, device_id, tombstone_ids)
        )

    def check_converged(self, tombstone_id: str, state: ConvergenceState | None = None) -> bool:
        """Check if all active devices have confirmed a tombstone."""
        if state is None:
            state = self._read_state()

        active_devices = [
            dev_id for dev_id in state.devices
            if dev_id not in state.blacklist
        ]

        if not active_devices:
            return True  # No active devices → trivially converged

        for dev_id in active_devices:
            dev_data = state.devices.get(dev_id, {})
            confirmed = dev_data.get("confirmed_tombstones", [])
            if tombstone_id not in confirmed:
                return False
        return True

    def gc_eligible(
        self,
        tombstone_id: str,
        deleted_at: float,
        now: float | None = None,
        retention_days: int = 90,
    ) -> bool:
        """Check if a tombstone is eligible for hard delete.

        Both conditions must be met:
        1. All active devices have converged.
        2. retention_days have passed since deletion.
        """
        if now is None:
            now = time.time()

        if now < deleted_at + retention_days * 86400:
            return False

        return self.check_converged(tombstone_id)

    def blacklist_device(self, device_id: str) -> bool:
        """Add a device to the blacklist (permanently offline).

        Returns True on success.
        """
        return self._retry_update(
            lambda state: self._apply_blacklist(state, device_id)
        )

    def get_state(self) -> ConvergenceState:
        """Read the current convergence state."""
        return self._read_state()

    # ── Internal ────────────────────────────────────────────────────

    def _read_state(self) -> ConvergenceState:
        """Read convergence state via callback."""
        if self._read_fn is None:
            return ConvergenceState()
        try:
            data, _etag = self._read_fn()
            return ConvergenceState.from_dict(data)
        except Exception:
            logger.warning("Failed to read convergence state", exc_info=True)
            return ConvergenceState()

    def _retry_update(self, apply_fn: Any) -> bool:
        """Retry loop with exponential backoff + jitter."""
        backoff = INITIAL_BACKOFF_S

        for attempt in range(MAX_RETRIES + 1):
            try:
                data, etag = self._read_fn() if self._read_fn else ({}, "")
                state = ConvergenceState.from_dict(data)
            except Exception:
                logger.warning(
                    f"Convergence read failed (attempt {attempt + 1})",
                    exc_info=True,
                )
                if attempt < MAX_RETRIES:
                    self._sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_S)
                continue

            apply_fn(state)

            if self._write_fn is None:
                return True

            try:
                success = self._write_fn(state.to_dict(), etag)
                if success:
                    return True
            except Exception:
                logger.warning(
                    f"Convergence write failed (attempt {attempt + 1})",
                    exc_info=True,
                )

            if attempt < MAX_RETRIES:
                jitter = random.uniform(0, backoff * 0.3)
                self._sleep(backoff + jitter)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_S)

        logger.error(f"Convergence update failed after {MAX_RETRIES + 1} attempts")
        return False

    def _sleep(self, seconds: float) -> None:
        """Sleep wrapper (overridable in tests)."""
        time.sleep(seconds)

    @staticmethod
    def _apply_report_seen(
        state: ConvergenceState,
        device_id: str,
        tombstone_ids: list[str],
    ) -> None:
        if device_id not in state.devices:
            state.devices[device_id] = {
                "last_seen": time.time(),
                "confirmed_tombstones": [],
            }
        dev = state.devices[device_id]
        dev["last_seen"] = time.time()
        existing = set(dev.get("confirmed_tombstones", []))
        existing.update(tombstone_ids)
        dev["confirmed_tombstones"] = sorted(existing)

    @staticmethod
    def _apply_blacklist(state: ConvergenceState, device_id: str) -> None:
        if device_id not in state.blacklist:
            state.blacklist.append(device_id)
