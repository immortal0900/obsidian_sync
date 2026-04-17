"""AdaptivePoller 단위 테스트.

실제 asyncio.sleep을 피하기 위해 `poll_once()`를 직접 호출하며,
`run()` 루프는 `asyncio.wait_for`를 monkeypatch하여 즉시 경과시킨다.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from src.config import POLL_MAX_INTERVAL, POLL_MIN_INTERVAL, POLL_START_INTERVAL
from src.drive_client import TokenInvalidError
from src.poller import AdaptivePoller

# ── 테스트 도우미 ───────────────────────────────────────────────────────


class FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


def make_http_error(status: int) -> HttpError:
    return HttpError(resp=FakeResp(status), content=b"")


def make_state(page_token: str | None = "T0") -> SimpleNamespace:
    return SimpleNamespace(page_token=page_token)


def make_watcher(last_event_age: float = 9999.0) -> MagicMock:
    watcher = MagicMock()
    watcher.last_event_age = MagicMock(return_value=last_event_age)
    return watcher


def make_engine() -> MagicMock:
    engine = MagicMock()
    engine.handle_remote_changes = MagicMock()
    return engine


def make_drive(get_changes_result: object) -> MagicMock:
    """get_changes의 반환/예외 동작을 설정하는 mock DriveClient."""
    drive = MagicMock()
    if callable(get_changes_result):
        drive.get_changes = MagicMock(side_effect=get_changes_result)
    elif isinstance(get_changes_result, Exception):
        drive.get_changes = MagicMock(side_effect=get_changes_result)
    else:
        drive.get_changes = MagicMock(return_value=get_changes_result)
    return drive


# ── 초기 상태 ───────────────────────────────────────────────────────────


class TestInitialState:
    def test_initial_interval_is_start(self) -> None:
        poller = AdaptivePoller(
            drive_client=make_drive(([], "T1")),
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        assert poller.current_interval == POLL_START_INTERVAL

    def test_token_invalid_signal_default_false(self) -> None:
        poller = AdaptivePoller(
            drive_client=make_drive(([], "T1")),
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        assert poller.token_invalid_signal is False


# ── 간격 규칙 ───────────────────────────────────────────────────────────


class TestBackoff:
    async def test_three_empty_polls_increase_by_factor_with_cap(self) -> None:
        drive = make_drive(([], "T1"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(last_event_age=9999.0),
            state=make_state(),
        )

        expected = [45.0, 67.5, 101.25, 120.0, 120.0]
        actual: list[float] = []

        for _ in range(5):
            await poller.poll_once()
            actual.append(poller.current_interval)

        assert actual == expected
        # 범위 방어
        for v in actual:
            assert POLL_MIN_INTERVAL <= v <= POLL_MAX_INTERVAL

    async def test_changes_reset_to_min(self) -> None:
        drive = make_drive(
            [
                ([], "T1"),
                ([{"file_id": "A", "removed": False, "file": {"name": "a.md"}}], "T2"),
            ]
        )
        # side_effect with list
        drive.get_changes = MagicMock(
            side_effect=[
                ([], "T1"),
                ([{"file_id": "A", "removed": False, "file": {"name": "a.md"}}], "T2"),
            ]
        )
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(9999.0),
            state=make_state(),
        )
        await poller.poll_once()
        assert poller.current_interval == 45.0

        await poller.poll_once()
        assert poller.current_interval == POLL_MIN_INTERVAL

    async def test_local_active_pins_to_start_interval(self) -> None:
        drive = make_drive(([], "T1"))
        watcher = make_watcher(last_event_age=30.0)
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=watcher,
            state=make_state(),
        )
        # 빈 결과 3회 모두 30초 고정
        for _ in range(3):
            await poller.poll_once()
            assert poller.current_interval == POLL_START_INTERVAL

    async def test_rate_limit_jumps_to_max(self) -> None:
        drive = make_drive(make_http_error(429))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(9999.0),
            state=make_state(),
        )
        await poller.poll_once()
        assert poller.current_interval == POLL_MAX_INTERVAL

    async def test_interval_bounded_entire_run(self) -> None:
        drive = make_drive(([], "T1"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(9999.0),
            state=make_state(),
        )
        for _ in range(20):
            await poller.poll_once()
            assert POLL_MIN_INTERVAL <= poller.current_interval <= POLL_MAX_INTERVAL


# ── 상태 갱신 ──────────────────────────────────────────────────────────


class TestStateUpdate:
    async def test_page_token_updated_on_success(self) -> None:
        drive = make_drive(([], "NEW_TOKEN"))
        state = make_state("OLD_TOKEN")
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=state,
        )
        await poller.poll_once()
        assert state.page_token == "NEW_TOKEN"

    async def test_handle_remote_changes_called_when_nonempty(self) -> None:
        changes = [{"file_id": "X", "removed": False, "file": {"name": "x.md"}}]
        drive = make_drive((changes, "T1"))
        engine = make_engine()
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=make_watcher(),
            state=make_state(),
        )
        await poller.poll_once()
        engine.handle_remote_changes.assert_called_once_with(changes)

    async def test_handle_remote_not_called_when_empty(self) -> None:
        drive = make_drive(([], "T1"))
        engine = make_engine()
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=make_watcher(),
            state=make_state(),
        )
        await poller.poll_once()
        engine.handle_remote_changes.assert_not_called()

    async def test_missing_page_token_skips_poll(self) -> None:
        drive = make_drive(([], "T1"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(page_token=None),
        )
        result = await poller.poll_once()
        drive.get_changes.assert_not_called()
        assert result is False


# ── TokenInvalidError ────────────────────────────────────────────────────


class TestTokenInvalid:
    async def test_token_invalid_does_not_exit_loop(self) -> None:
        drive = make_drive(TokenInvalidError("expired"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        # 예외가 상위로 올라오지 않아야 한다
        await poller.poll_once()
        assert poller.token_invalid_signal is True

    async def test_token_invalid_invokes_callback(self) -> None:
        drive = make_drive(TokenInvalidError("expired"))
        callback = MagicMock()

        async def on_token_invalid() -> None:
            callback()

        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
            on_token_invalid=on_token_invalid,
        )
        await poller.poll_once()
        callback.assert_called_once()
        assert poller.token_invalid_signal is True

    async def test_callback_exception_does_not_propagate(self) -> None:
        drive = make_drive(TokenInvalidError("expired"))

        async def on_token_invalid() -> None:
            raise RuntimeError("callback boom")

        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
            on_token_invalid=on_token_invalid,
        )
        # 상위로 올라오지 않음
        await poller.poll_once()
        assert poller.token_invalid_signal is True


# ── 오류 처리 ──────────────────────────────────────────────────────────


class TestErrorHandling:
    async def test_5xx_does_not_exit_loop(self) -> None:
        drive = make_drive(make_http_error(500))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        # 예외가 상위로 올라오지 않아야 하며 간격은 변동 없음
        before = poller.current_interval
        await poller.poll_once()
        assert poller.current_interval == before

    async def test_network_error_does_not_exit_loop(self) -> None:
        drive = make_drive(OSError("network down"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        await poller.poll_once()

    async def test_engine_exception_does_not_exit_loop(self) -> None:
        changes = [{"file_id": "X", "removed": False, "file": {"name": "x.md"}}]
        drive = make_drive((changes, "T1"))
        engine = make_engine()
        engine.handle_remote_changes = MagicMock(side_effect=RuntimeError("engine boom"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=make_watcher(),
            state=make_state(),
        )
        # 예외 삼키고 간격 업데이트까지 진행
        await poller.poll_once()

    async def test_engine_token_invalid_raises_signal(self) -> None:
        changes = [{"file_id": "X", "removed": False, "file": {"name": "x.md"}}]
        drive = make_drive((changes, "T1"))
        engine = make_engine()
        engine.handle_remote_changes = MagicMock(side_effect=TokenInvalidError("x"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=make_watcher(),
            state=make_state(),
        )
        await poller.poll_once()
        assert poller.token_invalid_signal is True


# ── run/stop ───────────────────────────────────────────────────────────


class TestRunStop:
    async def test_run_exits_on_stop(self) -> None:
        drive = make_drive(([], "T1"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        # 간격을 매우 짧게 고정 (테스트 동안 변동 방지)
        poller._current_interval = 0.005
        poller._update_interval = lambda **kwargs: None  # type: ignore[assignment]

        async def stopper() -> None:
            await asyncio.sleep(0.05)
            poller.stop()

        await asyncio.wait_for(
            asyncio.gather(poller.run(), stopper()),
            timeout=2.0,
        )
        assert drive.get_changes.call_count >= 1

    async def test_run_exits_on_cancel(self) -> None:
        drive = make_drive(([], "T1"))
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=make_engine(),
            local_watcher=make_watcher(),
            state=make_state(),
        )
        poller._current_interval = 0.005
        poller._update_interval = lambda **kwargs: None  # type: ignore[assignment]

        task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
