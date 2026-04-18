"""프로그램 진입점 — 구성요소 조립 + 기동/종료 흐름.

실행 순서:
1. `config.load_config()` — YAML 로드.
2. `DriveClient.authenticate()` — OAuth 인증.
3. `SyncState.load()` — 상태 파일 로드(True/False에 따라 분기).
4. `Reconciler.run()` 또는 `run_without_state()` → `SyncEngine.execute()` 루프.
5. `LocalWatcher.start()` + `AdaptivePoller.run()` 태스크 기동.
6. `asyncio.Event` 기반 shutdown 대기.
7. SIGINT/SIGTERM → watcher.stop → poller.stop → engine drain → state flush.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import SyncConfig, load_config
from src.conflict import ConflictResolver
from src.drive_client import DriveClient, TokenInvalidError
from src.local_watcher import LocalWatcher
from src.poller import AdaptivePoller
from src.reconciler import Reconciler
from src.state import SyncState
from src.sync_engine import SyncEngine
from src.trash import TrashManager

logger = logging.getLogger(__name__)

# 종료 시퀀스 타임아웃
ENGINE_DRAIN_TIMEOUT = 5.0
POLLER_STOP_TIMEOUT = 30.0
ENGINE_DRAIN_POLL_INTERVAL = 0.05


# ── 컨테이너 ─────────────────────────────────────────────────────────────


@dataclass
class AppContext:
    """main에서 조립한 구성요소 컨테이너."""

    config: SyncConfig
    drive: DriveClient
    state: SyncState
    engine: SyncEngine
    reconciler: Reconciler
    watcher: LocalWatcher
    poller: AdaptivePoller
    shutdown_event: asyncio.Event


# ── 로깅 설정 ────────────────────────────────────────────────────────────


def setup_logging(config: SyncConfig) -> None:
    """Rotating 파일 + 콘솔 로거를 설정한다. 중복 호출 시 무시한다."""
    root = logging.getLogger()
    if getattr(root, "_obsidian_sync_configured", False):
        return
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_file,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        logger.exception("로그 파일 핸들러 설정 실패 → 콘솔만 사용")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    root._obsidian_sync_configured = True  # type: ignore[attr-defined]


# ── 토큰 재발급 (P0-2) ───────────────────────────────────────────────────


class TokenRefreshCoordinator:
    """poller의 `TokenInvalidError` 콜백.

    - `asyncio.Lock`으로 중복 재진입 방지.
    - `reconciler.run_without_state()` 실행 후 `state.save(immediate=True)`.
    - 자격증명 실패(FileNotFoundError/PermissionError) 시 `shutdown_event` 세팅.
    """

    def __init__(
        self,
        drive: DriveClient,
        reconciler: Reconciler,
        engine: SyncEngine,
        state: SyncState,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._drive = drive
        self._reconciler = reconciler
        self._engine = engine
        self._state = state
        self._shutdown_event = shutdown_event
        self._lock = asyncio.Lock()

    @property
    def locked(self) -> bool:
        """재진입 중 여부 (테스트용)."""
        return self._lock.locked()

    async def __call__(self) -> None:
        """토큰 재발급 + 전체 재대조."""
        if self._lock.locked():
            logger.info("TokenRefresh: 이미 진행 중 — 중복 재진입 차단")
            return
        async with self._lock:
            logger.warning("TokenRefresh: run_without_state 재진입 시작")
            loop = asyncio.get_running_loop()
            try:
                actions = await loop.run_in_executor(
                    None, self._reconciler.run_without_state
                )
            except (FileNotFoundError, PermissionError):
                logger.exception("TokenRefresh: 자격증명 문제 → 종료 신호")
                self._shutdown_event.set()
                return
            except Exception:
                logger.exception("TokenRefresh: 예기치 못한 실패 → 종료 신호")
                self._shutdown_event.set()
                return

            for action in actions:
                try:
                    await loop.run_in_executor(None, self._engine.execute, action)
                except Exception:
                    logger.exception(f"TokenRefresh: action 실행 실패: {action}")

            try:
                self._state.save(immediate=True)
            except Exception:
                logger.exception("TokenRefresh: state.save 실패")
            logger.info("TokenRefresh: 재진입 완료")


# ── 초기 대조 ────────────────────────────────────────────────────────────


async def initial_reconcile(
    reconciler: Reconciler,
    engine: SyncEngine,
    state_loaded: bool,
) -> None:
    """초기 대조: `state_loaded`에 따라 run 또는 run_without_state."""
    loop = asyncio.get_running_loop()
    if state_loaded:
        actions = await loop.run_in_executor(None, reconciler.run)
    else:
        actions = await loop.run_in_executor(None, reconciler.run_without_state)
    logger.info(f"초기 reconcile: {len(actions)}개 action")
    for action in actions:
        await loop.run_in_executor(None, engine.execute, action)


# ── 종료 시퀀스 ─────────────────────────────────────────────────────────


async def wait_engine_idle(
    engine: SyncEngine,
    timeout: float = ENGINE_DRAIN_TIMEOUT,
) -> None:
    """engine의 잠금이 풀릴 때까지 대기한다 (pending 큐 소진)."""
    deadline = time.monotonic() + timeout
    while engine.lock:
        if time.monotonic() > deadline:
            logger.warning("engine drain timeout — 강제 진행")
            return
        await asyncio.sleep(ENGINE_DRAIN_POLL_INTERVAL)


async def shutdown(
    watcher: LocalWatcher,
    poller: AdaptivePoller,
    poll_task: asyncio.Task[Any] | None,
    engine: SyncEngine,
    state: SyncState,
) -> None:
    """종료 시퀀스 — watcher → poller → engine drain → state flush 순."""
    logger.info("Shutdown: watcher 중지")
    try:
        watcher.stop()
    except Exception:
        logger.exception("watcher.stop 실패")

    logger.info("Shutdown: poller 중지")
    try:
        poller.stop()
    except Exception:
        logger.exception("poller.stop 실패")

    if poll_task is not None:
        try:
            await asyncio.wait_for(poll_task, timeout=POLLER_STOP_TIMEOUT)
        except TimeoutError:
            logger.warning("poller 태스크 타임아웃 → cancel")
            poll_task.cancel()
            try:
                await poll_task
            except (asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("poller 태스크 대기 중 예외")

    await wait_engine_idle(engine)

    logger.info("Shutdown: state flush")
    try:
        state.save(immediate=True)
    except Exception:
        logger.exception("state.save 실패")


# ── 시그널 ───────────────────────────────────────────────────────────────


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """SIGINT/SIGTERM → shutdown_event.set(). Windows fallback 포함."""

    def _trigger() -> None:
        if not shutdown_event.is_set():
            logger.info("시그널 수신 → shutdown 요청")
            shutdown_event.set()

    signals = (signal.SIGINT, getattr(signal, "SIGTERM", None))
    fallback_needed = False
    for sig in signals:
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _trigger)
        except (NotImplementedError, RuntimeError, ValueError):
            fallback_needed = True

    if fallback_needed:
        _install_os_signal_fallback(loop, shutdown_event)


def _install_os_signal_fallback(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """Windows 환경용 `signal.signal` fallback."""

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        try:
            loop.call_soon_threadsafe(shutdown_event.set)
        except RuntimeError:
            shutdown_event.set()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            continue


# ── 메인 오케스트레이션 ────────────────────────────────────────────────


def build_context(
    config: SyncConfig,
    shutdown_event: asyncio.Event,
    on_token_invalid: Callable[[], Awaitable[None]] | None = None,
) -> AppContext:
    """구성요소를 조립한다. 인증/load는 호출자가 수행."""
    drive = DriveClient(config)
    state = SyncState(config)
    conflict_resolver = ConflictResolver(config.device_id, config.vault_path)
    trash_manager = TrashManager(config.vault_path)
    engine = SyncEngine(drive, state, conflict_resolver, trash_manager=trash_manager)
    reconciler = Reconciler(state, drive)
    watcher = LocalWatcher(
        config.vault_path, engine, debounce_seconds=config.debounce_seconds
    )
    poller = AdaptivePoller(
        drive, engine, watcher, state, on_token_invalid=on_token_invalid
    )
    return AppContext(
        config=config,
        drive=drive,
        state=state,
        engine=engine,
        reconciler=reconciler,
        watcher=watcher,
        poller=poller,
        shutdown_event=shutdown_event,
    )


async def run_app(ctx: AppContext) -> int:
    """조립된 AppContext를 실행한다 (테스트에서 직접 호출 가능).

    인증과 초기 reconcile, watcher/poller 기동, shutdown 대기까지 수행한다.
    """
    loop = asyncio.get_running_loop()

    # 인증
    try:
        await loop.run_in_executor(None, ctx.drive.authenticate)
    except FileNotFoundError:
        logger.exception("인증 자격증명 누락")
        return 1
    except Exception:
        logger.exception("Drive 인증 실패")
        return 1

    # 상태 로드 — True: run(), False: run_without_state()
    state_loaded = ctx.state.load()

    # 초기 reconcile
    try:
        await initial_reconcile(ctx.reconciler, ctx.engine, state_loaded)
    except TokenInvalidError:
        logger.warning("초기 reconcile TokenInvalidError → run_without_state 재시도")
        try:
            await initial_reconcile(ctx.reconciler, ctx.engine, False)
        except Exception:
            logger.exception("run_without_state 실패")
            return 1
    except Exception:
        logger.exception("초기 reconcile 실패")
        return 1

    # watcher + poller 기동
    ctx.watcher.start()
    poll_task: asyncio.Task[Any] = asyncio.create_task(ctx.poller.run())

    install_signal_handlers(loop, ctx.shutdown_event)

    logger.info("기동 완료 — shutdown_event 대기")
    try:
        await ctx.shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("run_app 태스크 취소")
    finally:
        await shutdown(ctx.watcher, ctx.poller, poll_task, ctx.engine, ctx.state)

    logger.info("프로그램 종료 (exit code 0)")
    return 0


async def main(config_path: str | Path = "config.yaml") -> int:
    """프로그램 진입점. 종료 코드 반환."""
    try:
        config = load_config(config_path)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return code

    setup_logging(config)

    shutdown_event = asyncio.Event()
    # token_refresh는 reconciler/engine/state를 먼저 조립한 뒤 구성된다.
    stub_ctx = build_context(config, shutdown_event, on_token_invalid=None)
    token_refresh = TokenRefreshCoordinator(
        stub_ctx.drive,
        stub_ctx.reconciler,
        stub_ctx.engine,
        stub_ctx.state,
        shutdown_event,
    )
    # poller에 콜백을 주입하여 최종 컨텍스트 조립
    ctx = AppContext(
        config=stub_ctx.config,
        drive=stub_ctx.drive,
        state=stub_ctx.state,
        engine=stub_ctx.engine,
        reconciler=stub_ctx.reconciler,
        watcher=stub_ctx.watcher,
        poller=AdaptivePoller(
            stub_ctx.drive,
            stub_ctx.engine,
            stub_ctx.watcher,
            stub_ctx.state,
            on_token_invalid=token_refresh,
        ),
        shutdown_event=shutdown_event,
    )
    return await run_app(ctx)


def run(argv: list[str] | None = None) -> int:
    """CLI 엔트리 — asyncio.run 래퍼.

    `--config PATH` 인자로 설정 파일을 지정할 수 있다 (기본: config.yaml).
    동일 프로젝트 폴더에서 여러 볼트를 운영할 때 쓴다.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="obsidian-sync",
        description="Obsidian ↔ Google Drive 양방향 동기화 데몬",
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="설정 파일 경로 (기본: config.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(main(args.config))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — 종료")
        return 0


if __name__ == "__main__":
    sys.exit(run())
