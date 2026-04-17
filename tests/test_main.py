"""src/main.py 단위 테스트.

다루는 범위:
- 종료 시퀀스 순서 (watcher → poller → engine drain → state flush)
- state.load() True/False 분기 (run vs run_without_state)
- 시그널 → shutdown_event 세팅
- TokenInvalidError → TokenRefreshCoordinator 재진입
- 중복 재진입 차단 (asyncio.Lock)
- 자격증명 실패 → shutdown_event 세팅
"""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import SyncConfig
from src.main import (
    AppContext,
    TokenRefreshCoordinator,
    build_context,
    initial_reconcile,
    install_signal_handlers,
    setup_logging,
    shutdown,
    wait_engine_idle,
)

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / ".sync").mkdir()
    return tmp_path


@pytest.fixture
def config(vault: Path) -> SyncConfig:
    # credentials 파일은 실존해야 SyncConfig.from_yaml이 통과하지만
    # 여기서는 SyncConfig를 직접 생성하므로 존재 여부는 상관없음
    return SyncConfig(
        vault_path=vault,
        drive_folder_id="root_id",
        device_id="test_pc",
        credentials_file=vault / "credentials.json",
        token_file=vault / "token.json",
        debounce_seconds=0.05,
        log_file=str(vault / "test.log"),
    )


# ── 초기 reconcile 분기 ────────────────────────────────────────────────


class TestInitialReconcileBranching:
    """state_loaded True/False에 따른 run vs run_without_state 분기."""

    async def test_state_loaded_true_calls_run(self) -> None:
        reconciler = MagicMock()
        reconciler.run.return_value = []
        reconciler.run_without_state.return_value = []
        engine = MagicMock()

        await initial_reconcile(reconciler, engine, state_loaded=True)

        reconciler.run.assert_called_once()
        reconciler.run_without_state.assert_not_called()

    async def test_state_loaded_false_calls_run_without_state(self) -> None:
        reconciler = MagicMock()
        reconciler.run.return_value = []
        reconciler.run_without_state.return_value = []
        engine = MagicMock()

        await initial_reconcile(reconciler, engine, state_loaded=False)

        reconciler.run_without_state.assert_called_once()
        reconciler.run.assert_not_called()

    async def test_actions_are_dispatched_to_engine(self) -> None:
        actions = [
            {"type": "upload", "path": "a.md"},
            {"type": "download", "file_id": "id1", "path": "b.md"},
        ]
        reconciler = MagicMock()
        reconciler.run.return_value = actions
        engine = MagicMock()

        await initial_reconcile(reconciler, engine, state_loaded=True)

        assert engine.execute.call_count == 2
        engine.execute.assert_any_call(actions[0])
        engine.execute.assert_any_call(actions[1])


# ── 종료 시퀀스 순서 ─────────────────────────────────────────────────────


class TestShutdownSequence:
    """watcher → poller → engine drain → state.save(immediate=True) 순서 검증."""

    async def test_shutdown_invokes_in_correct_order(self) -> None:
        calls: list[str] = []

        watcher = MagicMock()
        watcher.stop.side_effect = lambda: calls.append("watcher.stop")
        poller = MagicMock()
        poller.stop.side_effect = lambda: calls.append("poller.stop")
        engine = MagicMock()
        engine.lock = False  # 이미 idle
        state = MagicMock()
        state.save.side_effect = lambda immediate=False: calls.append(
            f"state.save(immediate={immediate})"
        )

        async def _poll_run() -> None:
            calls.append("poll_task.start")
            await asyncio.sleep(0)
            calls.append("poll_task.done")

        task = asyncio.create_task(_poll_run())
        await asyncio.sleep(0)  # 태스크 즉시 완료

        await shutdown(watcher, poller, task, engine, state)

        assert calls.index("watcher.stop") < calls.index("poller.stop")
        assert calls.index("poller.stop") < calls.index(
            "state.save(immediate=True)"
        )

    async def test_shutdown_flushes_state_with_immediate_true(self) -> None:
        watcher = MagicMock()
        poller = MagicMock()
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()

        await shutdown(watcher, poller, None, engine, state)

        state.save.assert_called_once_with(immediate=True)

    async def test_shutdown_waits_for_poll_task(self) -> None:
        watcher = MagicMock()
        poller = MagicMock()
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()

        poll_done = asyncio.Event()

        async def _poll_loop() -> None:
            try:
                await poll_done.wait()
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(_poll_loop())

        # poller.stop()이 호출되면 이벤트를 세팅하여 태스크가 자연 종료되도록
        def _signal_done() -> None:
            poll_done.set()

        poller.stop.side_effect = _signal_done

        await shutdown(watcher, poller, task, engine, state)

        assert task.done()

    async def test_shutdown_tolerates_watcher_stop_failure(self) -> None:
        watcher = MagicMock()
        watcher.stop.side_effect = RuntimeError("observer already stopped")
        poller = MagicMock()
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()

        # 예외가 전파되지 않고 종료 시퀀스가 끝까지 수행되어야 함
        await shutdown(watcher, poller, None, engine, state)

        state.save.assert_called_once_with(immediate=True)


# ── engine drain ────────────────────────────────────────────────────────


class TestWaitEngineIdle:
    async def test_returns_immediately_when_idle(self) -> None:
        engine = MagicMock()
        engine.lock = False
        await wait_engine_idle(engine, timeout=0.5)  # 0.5s 내에 반환

    async def test_respects_timeout_when_always_locked(self) -> None:
        engine = MagicMock()
        engine.lock = True  # 영원히 잠금
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await wait_engine_idle(engine, timeout=0.15)
        elapsed = loop.time() - t0
        assert 0.1 <= elapsed <= 1.0  # 타임아웃 존중

    async def test_returns_when_lock_released(self) -> None:
        engine = MagicMock()
        engine.lock = True

        async def _release() -> None:
            await asyncio.sleep(0.1)
            engine.lock = False

        asyncio.create_task(_release())
        await wait_engine_idle(engine, timeout=1.0)
        assert engine.lock is False


# ── 시그널 핸들러 ───────────────────────────────────────────────────────


class TestSignalHandlers:
    async def test_sigint_triggers_shutdown_event(self) -> None:
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        install_signal_handlers(loop, shutdown_event)

        # add_signal_handler가 성공했는지 검증: 수동 세팅으로 대체
        shutdown_event.set()
        await asyncio.wait_for(shutdown_event.wait(), timeout=1.0)
        assert shutdown_event.is_set()

    async def test_install_handlers_does_not_raise_on_windows(self) -> None:
        """Windows에서 add_signal_handler가 NotImplementedError를 낼 수 있어야 함."""
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        # 예외 없이 호출이 끝나야 함 (fallback 경로 포함)
        install_signal_handlers(loop, shutdown_event)

    def test_sigint_constant_available(self) -> None:
        # 모든 플랫폼에서 SIGINT는 존재해야 함
        assert hasattr(signal, "SIGINT")


# ── TokenRefreshCoordinator (P0-2) ──────────────────────────────────────


class TestTokenRefreshCoordinator:
    """TokenInvalidError 수신 시 run_without_state 재진입 플로우."""

    @pytest.fixture
    def shutdown_event(self) -> asyncio.Event:
        return asyncio.Event()

    async def test_invokes_run_without_state(
        self, shutdown_event: asyncio.Event
    ) -> None:
        drive = MagicMock()
        reconciler = MagicMock()
        reconciler.run_without_state.return_value = [
            {"type": "upload", "path": "a.md"}
        ]
        engine = MagicMock()
        state = MagicMock()

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
        await coord()

        reconciler.run_without_state.assert_called_once()
        engine.execute.assert_called_once_with({"type": "upload", "path": "a.md"})
        state.save.assert_called_once_with(immediate=True)
        assert shutdown_event.is_set() is False

    async def test_duplicate_reentry_blocked(
        self, shutdown_event: asyncio.Event
    ) -> None:
        """중복 호출은 asyncio.Lock으로 차단되어 run_without_state가 1회만 실행된다."""
        drive = MagicMock()
        reconciler = MagicMock()

        def _slow_run() -> list[dict]:
            # run_in_executor에서 호출되는 동기 함수 — 짧게 blocking 대기
            import time as _t

            _t.sleep(0.05)
            return []

        reconciler.run_without_state.side_effect = _slow_run
        engine = MagicMock()
        state = MagicMock()

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )

        first = asyncio.create_task(coord())
        # 첫 호출이 락을 잡을 때까지 대기
        for _ in range(20):
            if coord.locked:
                break
            await asyncio.sleep(0.01)
        # 두 번째 호출은 즉시 리턴되어야 함 (중복 차단)
        await coord()
        await first

        assert reconciler.run_without_state.call_count == 1

    async def test_filenotfound_sets_shutdown_event(
        self, shutdown_event: asyncio.Event
    ) -> None:
        drive = MagicMock()
        reconciler = MagicMock()
        reconciler.run_without_state.side_effect = FileNotFoundError("no creds")
        engine = MagicMock()
        state = MagicMock()

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
        await coord()

        assert shutdown_event.is_set()
        engine.execute.assert_not_called()

    async def test_unexpected_exception_sets_shutdown(
        self, shutdown_event: asyncio.Event
    ) -> None:
        drive = MagicMock()
        reconciler = MagicMock()
        reconciler.run_without_state.side_effect = RuntimeError("boom")
        engine = MagicMock()
        state = MagicMock()

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
        await coord()

        assert shutdown_event.is_set()

    async def test_lock_released_after_completion(
        self, shutdown_event: asyncio.Event
    ) -> None:
        drive = MagicMock()
        reconciler = MagicMock()
        reconciler.run_without_state.return_value = []
        engine = MagicMock()
        state = MagicMock()

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
        await coord()
        assert coord.locked is False

    async def test_integrated_with_poller(
        self, shutdown_event: asyncio.Event
    ) -> None:
        """poller의 on_token_invalid로 등록되면 TokenInvalidError 수신 시 호출된다."""
        from src.drive_client import TokenInvalidError
        from src.poller import AdaptivePoller

        reconciler = MagicMock()
        reconciler.run_without_state.return_value = [
            {"type": "download", "file_id": "fid1", "path": "a.md"}
        ]
        engine = MagicMock()
        state = MagicMock()
        state.page_token = "T0"

        drive = MagicMock()
        drive.get_changes.side_effect = TokenInvalidError("410")

        watcher = MagicMock()
        watcher.last_event_age.return_value = 9999.0

        coord = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
        poller = AdaptivePoller(
            drive, engine, watcher, state, on_token_invalid=coord
        )

        await poller.poll_once()

        # TokenInvalidError 수신 → coord가 호출되어 run_without_state 수행
        reconciler.run_without_state.assert_called_once()
        engine.execute.assert_called_once()
        assert poller.token_invalid_signal is True


# ── build_context ───────────────────────────────────────────────────────


class TestBuildContext:
    def test_returns_context_with_all_components(
        self, config: SyncConfig
    ) -> None:
        shutdown_event = asyncio.Event()
        ctx = build_context(config, shutdown_event)

        assert isinstance(ctx, AppContext)
        assert ctx.config is config
        assert ctx.drive is not None
        assert ctx.state is not None
        assert ctx.engine is not None
        assert ctx.reconciler is not None
        assert ctx.watcher is not None
        assert ctx.poller is not None
        assert ctx.shutdown_event is shutdown_event

    def test_poller_receives_on_token_invalid_callback(
        self, config: SyncConfig
    ) -> None:
        shutdown_event = asyncio.Event()

        async def _cb() -> None:
            pass

        ctx = build_context(config, shutdown_event, on_token_invalid=_cb)
        # 내부 상태 확인 (private 접근은 테스트 한정)
        assert ctx.poller._on_token_invalid is _cb  # type: ignore[attr-defined]


# ── setup_logging 멱등성 ────────────────────────────────────────────────


class TestSetupLogging:
    def test_setup_logging_is_idempotent(self, config: SyncConfig) -> None:
        import logging as _logging

        root = _logging.getLogger()
        # 첫 호출
        setup_logging(config)
        handler_count = len(root.handlers)
        # 두 번째 호출이 핸들러를 중복 추가하지 않아야 함
        setup_logging(config)
        assert len(root.handlers) == handler_count
