"""적응형 폴링 (Drive Changes API).

watchdog로 잡히지 않는 클라우드 측 변경을 감지한다.
활동성에 따라 10~120초 구간에서 간격을 자동 조정한다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from googleapiclient.errors import HttpError

from src.config import (
    POLL_BACKOFF_FACTOR,
    POLL_MAX_INTERVAL,
    POLL_MIN_INTERVAL,
    POLL_START_INTERVAL,
)
from src.drive_client import DriveClient, TokenInvalidError

if TYPE_CHECKING:
    from src.local_watcher import LocalWatcher
    from src.state import SyncState
    from src.sync_engine import SyncEngine

logger = logging.getLogger(__name__)

# 로컬 활동 판단 임계값 (spec §5-5 / drive-integration.md)
LOCAL_ACTIVITY_WINDOW_SECONDS = 60.0


class AdaptivePoller:
    """Drive Changes API를 적응형 간격으로 폴링한다.

    간격 결정 규칙 (우선순위 순):
        1. 직전 폴링에서 변경 발견 → POLL_MIN_INTERVAL (10s)
        2. `local_watcher.last_event_age() < 60s` → POLL_START_INTERVAL (30s)
        3. 양쪽 조용 → 간격 *= POLL_BACKOFF_FACTOR, 상한 POLL_MAX_INTERVAL
        4. Drive 429 수신 → 즉시 POLL_MAX_INTERVAL로 고정
    """

    def __init__(
        self,
        drive_client: DriveClient,
        sync_engine: SyncEngine,
        local_watcher: LocalWatcher,
        state: SyncState,
        *,
        on_token_invalid: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._drive = drive_client
        self._sync_engine = sync_engine
        self._local_watcher = local_watcher
        self._state = state
        self._on_token_invalid = on_token_invalid

        self._current_interval: float = float(POLL_START_INTERVAL)
        self._stop_event: asyncio.Event | None = None
        self._token_invalid_signal = False

    # ── 프로퍼티 ─────────────────────────────────────────────────────────

    @property
    def current_interval(self) -> float:
        """다음 폴링까지 대기할 간격(초)."""
        return self._current_interval

    @property
    def token_invalid_signal(self) -> bool:
        """TokenInvalidError가 폴링 중 한 번이라도 발생했는지."""
        return self._token_invalid_signal

    # ── 단일 폴링 ────────────────────────────────────────────────────────

    async def poll_once(self) -> bool:
        """1회 폴링을 수행한다. 간격을 갱신하고 변경 존재 여부를 반환한다."""
        page_token = self._state.page_token
        if not page_token:
            logger.debug("page_token 없음 → 폴링 건너뜀")
            # 간격은 유지. 상위가 reconciler.run_without_state로 토큰 발급해야 함.
            return False

        try:
            changes, new_token = self._drive.get_changes(page_token)
        except TokenInvalidError:
            logger.warning("poller: page_token 무효 — 재발급 신호")
            self._token_invalid_signal = True
            if self._on_token_invalid is not None:
                await self._invoke_token_invalid()
            # 루프는 탈출하지 않는다. 간격은 그대로 유지.
            return False
        except HttpError as e:
            if _is_rate_limit(e):
                logger.warning("poller: 429 감지 → 간격을 최대값으로 고정")
                self._current_interval = float(POLL_MAX_INTERVAL)
                return False
            logger.exception("poller: get_changes HTTP 오류")
            return False
        except (OSError, TimeoutError):
            logger.exception("poller: 네트워크 오류")
            return False

        # 토큰 갱신
        if new_token:
            self._state.page_token = new_token

        # 변경 디스패치
        if changes:
            try:
                self._sync_engine.handle_remote_changes(changes)
            except TokenInvalidError:
                logger.warning("poller: handle_remote_changes에서 토큰 무효 감지")
                self._token_invalid_signal = True
                if self._on_token_invalid is not None:
                    await self._invoke_token_invalid()
                return False
            except Exception:
                logger.exception("poller: handle_remote_changes 실패")

        self._update_interval(found_changes=bool(changes))
        return bool(changes)

    async def _invoke_token_invalid(self) -> None:
        try:
            assert self._on_token_invalid is not None
            await self._on_token_invalid()
        except Exception:
            logger.exception("on_token_invalid 콜백 실패")

    def _update_interval(self, *, found_changes: bool) -> None:
        """간격 결정 규칙(우선순위 1~3)을 적용한다."""
        if found_changes:
            self._current_interval = float(POLL_MIN_INTERVAL)
            return

        if self._local_watcher.last_event_age() < LOCAL_ACTIVITY_WINDOW_SECONDS:
            self._current_interval = float(POLL_START_INTERVAL)
            return

        new_interval = self._current_interval * POLL_BACKOFF_FACTOR
        self._current_interval = min(new_interval, float(POLL_MAX_INTERVAL))
        # 하한도 방어
        self._current_interval = max(self._current_interval, float(POLL_MIN_INTERVAL))

    # ── 루프 ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """폴링 루프. `stop()` 또는 Task cancel로 종료한다."""
        self._stop_event = asyncio.Event()
        logger.info(
            f"AdaptivePoller 시작 (초기 간격 {self._current_interval}s, "
            f"범위 [{POLL_MIN_INTERVAL}, {POLL_MAX_INTERVAL}])"
        )

        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._current_interval,
                    )
                    # 이벤트가 set → 루프 종료
                    return
                except TimeoutError:
                    pass

                await self.poll_once()
        except asyncio.CancelledError:
            logger.info("AdaptivePoller: 태스크 취소")
            raise
        finally:
            logger.info("AdaptivePoller 종료")

    def stop(self) -> None:
        """실행 중인 루프를 종료 요청한다."""
        if self._stop_event is not None:
            self._stop_event.set()


# ── 유틸 ─────────────────────────────────────────────────────────────────


def _is_rate_limit(error: HttpError) -> bool:
    """HttpError가 429인지 판정한다."""
    status: Any = getattr(error.resp, "status", None)
    if status is None:
        return False
    try:
        return int(status) == 429
    except (TypeError, ValueError):
        return False
