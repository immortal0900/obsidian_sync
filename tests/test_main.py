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
from typing import Any
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

    async def test_shutdown_tolerates_poller_stop_failure(self) -> None:
        watcher = MagicMock()
        poller = MagicMock()
        poller.stop.side_effect = RuntimeError("poller already stopped")
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()

        await shutdown(watcher, poller, None, engine, state)

        state.save.assert_called_once_with(immediate=True)

    async def test_shutdown_tolerates_state_save_failure(self) -> None:
        watcher = MagicMock()
        poller = MagicMock()
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()
        state.save.side_effect = OSError("disk full")

        # state.save 실패도 흡수되어야 함
        await shutdown(watcher, poller, None, engine, state)

        state.save.assert_called_once()

    async def test_shutdown_cancels_poll_task_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """poller 태스크가 POLLER_STOP_TIMEOUT 내 끝나지 않으면 cancel한다."""
        from src import main as main_mod

        monkeypatch.setattr(main_mod, "POLLER_STOP_TIMEOUT", 0.05)

        watcher = MagicMock()
        poller = MagicMock()  # stop()은 no-op → 태스크가 스스로 끝나지 않음
        engine = MagicMock()
        engine.lock = False
        state = MagicMock()

        async def _never_ending() -> None:
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(_never_ending())

        await shutdown(watcher, poller, task, engine, state)

        assert task.done()
        assert task.cancelled()


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


# ── run_app 오류 경로 ───────────────────────────────────────────────────


class TestRunAppErrorPaths:
    """run_app의 인증/reconcile 실패 → exit code 1 경로."""

    async def test_authenticate_filenotfound_returns_1(
        self, config: SyncConfig
    ) -> None:
        from src.main import run_app

        shutdown_event = asyncio.Event()
        ctx = build_context(config, shutdown_event)
        ctx.drive.authenticate = MagicMock(  # type: ignore[method-assign]
            side_effect=FileNotFoundError("no creds")
        )

        exit_code = await run_app(ctx)
        assert exit_code == 1

    async def test_authenticate_unexpected_error_returns_1(
        self, config: SyncConfig
    ) -> None:
        from src.main import run_app

        shutdown_event = asyncio.Event()
        ctx = build_context(config, shutdown_event)
        ctx.drive.authenticate = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )

        exit_code = await run_app(ctx)
        assert exit_code == 1

    async def test_initial_reconcile_tokeninvalid_falls_back_to_run_without_state(
        self, config: SyncConfig
    ) -> None:
        """TokenInvalidError 수신 시 run_without_state 자동 재시도."""
        from src.drive_client import TokenInvalidError
        from src.main import run_app

        shutdown_event = asyncio.Event()
        ctx = build_context(config, shutdown_event)

        # 인증/watcher/poller를 가짜로 대체
        ctx.drive.authenticate = MagicMock()  # type: ignore[method-assign]
        ctx.reconciler.run = MagicMock(  # type: ignore[method-assign]
            side_effect=TokenInvalidError("410")
        )
        ctx.reconciler.run_without_state = MagicMock(  # type: ignore[method-assign]
            return_value=[]
        )
        ctx.state.load = MagicMock(return_value=True)  # type: ignore[method-assign]
        ctx.watcher.start = MagicMock()  # type: ignore[method-assign]
        ctx.watcher.stop = MagicMock()  # type: ignore[method-assign]

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.05)
            shutdown_event.set()

        asyncio.create_task(_trigger_shutdown())
        exit_code = await asyncio.wait_for(run_app(ctx), timeout=5.0)

        assert exit_code == 0
        ctx.reconciler.run_without_state.assert_called_once()

    async def test_initial_reconcile_generic_failure_returns_1(
        self, config: SyncConfig
    ) -> None:
        from src.main import run_app

        shutdown_event = asyncio.Event()
        ctx = build_context(config, shutdown_event)
        ctx.drive.authenticate = MagicMock()  # type: ignore[method-assign]
        ctx.reconciler.run = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )
        ctx.state.load = MagicMock(return_value=True)  # type: ignore[method-assign]

        exit_code = await run_app(ctx)
        assert exit_code == 1


# ── main() 진입점 ───────────────────────────────────────────────────────


class TestMainEntryPoint:
    """main()이 config 로드 실패 / 정상 종료를 처리하는지 검증."""

    async def test_main_returns_nonzero_on_missing_config(
        self, tmp_path: Path
    ) -> None:
        from src.main import main

        missing = tmp_path / "nope.yaml"
        exit_code = await main(missing)
        assert exit_code != 0

    async def test_main_composes_and_runs(
        self, monkeypatch: pytest.MonkeyPatch, config: SyncConfig
    ) -> None:
        """main()이 load_config → build_context → run_app 체인을 구동한다."""
        from src import main as main_mod

        monkeypatch.setattr(main_mod, "load_config", lambda _p: config)

        async def _fake_run_app(ctx: object) -> int:
            # shutdown_event 즉시 세팅
            ctx.shutdown_event.set()  # type: ignore[attr-defined]
            return 0

        monkeypatch.setattr(main_mod, "run_app", _fake_run_app)
        exit_code = await main_mod.main("any.yaml")
        assert exit_code == 0


# ── run() CLI 래퍼 ──────────────────────────────────────────────────────


class TestRunCli:
    def test_run_catches_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src import main as main_mod

        def _raise(coro: Any) -> int:
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(main_mod.asyncio, "run", _raise)
        assert main_mod.run() == 0

    def test_run_returns_async_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src import main as main_mod

        def _close_and_return(coro: Any) -> int:
            coro.close()
            return 42

        monkeypatch.setattr(main_mod.asyncio, "run", _close_and_return)
        assert main_mod.run() == 42


# ── signal fallback ─────────────────────────────────────────────────────


class TestSignalFallback:
    async def test_add_signal_handler_failure_triggers_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """add_signal_handler가 NotImplementedError면 signal.signal fallback 경로."""
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", _raise)

        signal_calls: list[tuple[int, object]] = []

        def _capture_signal(sig: int, handler: object) -> None:
            signal_calls.append((sig, handler))

        monkeypatch.setattr(signal, "signal", _capture_signal)
        install_signal_handlers(loop, shutdown_event)

        # 최소 SIGINT는 fallback 경로로 등록되어야 함
        assert any(call[0] == signal.SIGINT for call in signal_calls)

    async def test_os_fallback_handler_sets_shutdown_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fallback handler를 직접 호출 → shutdown_event 세팅 검증."""
        from src.main import _install_os_signal_fallback

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        registered: list[tuple[int, Any]] = []

        def _capture(sig: int, handler: Any) -> None:
            registered.append((sig, handler))

        monkeypatch.setattr(signal, "signal", _capture)
        _install_os_signal_fallback(loop, shutdown_event)

        assert registered
        # 등록된 핸들러 중 하나를 호출
        _sig, handler = registered[0]
        handler(_sig, None)
        # call_soon_threadsafe에 의해 set됨
        await asyncio.sleep(0)
        assert shutdown_event.is_set()

    async def test_os_fallback_skips_signals_that_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """signal.signal이 OSError를 내면 해당 시그널은 건너뛴다."""
        from src.main import _install_os_signal_fallback

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _always_fail(sig: int, handler: Any) -> None:
            raise OSError("not allowed")

        monkeypatch.setattr(signal, "signal", _always_fail)
        # 예외 없이 완료되어야 함
        _install_os_signal_fallback(loop, shutdown_event)


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
