"""watchdog 기반 로컬 파일 감시.

파일 이벤트를 파일 경로별 디바운스 타이머로 집계한 뒤
`sync_engine`에 1회만 전달한다. 삭제 이벤트는 디바운스 없이 즉시 전파한다.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from src.config import SYNC_STATE_DIR, should_ignore

if TYPE_CHECKING:
    from src.sync_engine import SyncEngine

logger = logging.getLogger(__name__)

class LocalWatcher(FileSystemEventHandler):
    """볼트 디렉토리의 파일 변경을 감시하여 sync_engine에 전달한다.

    - 같은 경로에 대한 연속 이벤트는 `debounce_seconds` 내에 1회로 집계된다.
    - 삭제 이벤트는 즉시 전파한다.
    - 이동 이벤트는 delete(old) + create(new)로 분해한다.
    """

    def __init__(
        self,
        vault_path: Path | str,
        sync_engine: SyncEngine,
        debounce_seconds: float = 2.0,
    ) -> None:
        super().__init__()
        self._vault_path = Path(vault_path).resolve()
        self._sync_engine = sync_engine
        self._debounce_seconds = debounce_seconds

        self._observer: Any = None
        self._use_polling_fallback = False

        # 파일 경로별 디바운스 타이머
        self._timers: dict[str, threading.Timer] = {}
        self._timers_lock = threading.Lock()

        # 마지막 이벤트 시각(monotonic). 초기값 None → 무한대 반환.
        self._last_event_monotonic: float | None = None

    # ── 생명주기 ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """네이티브 옵저버를 기동한다. 실패 시 PollingObserver로 fallback."""
        try:
            observer: Any = Observer()
            observer.schedule(self, str(self._vault_path), recursive=True)
            observer.start()
        except (OSError, RuntimeError) as e:
            logger.warning(f"네이티브 옵저버 실패 → PollingObserver fallback: {e}")
            observer = PollingObserver()
            observer.schedule(self, str(self._vault_path), recursive=True)
            observer.start()
            self._use_polling_fallback = True

        self._observer = observer
        logger.info(f"LocalWatcher 시작: {self._vault_path}")

    def stop(self) -> None:
        """진행 중인 타이머를 취소하고 옵저버를 종료한다."""
        with self._timers_lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5.0)
            except RuntimeError:
                logger.debug("옵저버 join 실패 — 이미 종료됨")
            self._observer = None
        logger.info("LocalWatcher 종료")

    # ── watchdog 이벤트 핸들러 ───────────────────────────────────────────

    def on_created(self, event: Any) -> None:
        self._enqueue_with_debounce(event, "created")

    def on_modified(self, event: Any) -> None:
        self._enqueue_with_debounce(event, "modified")

    def on_deleted(self, event: Any) -> None:
        """삭제는 디바운스 없이 즉시 전파한다."""
        if getattr(event, "is_directory", False):
            return

        rel = self._to_relative(getattr(event, "src_path", ""))
        if rel is None or self._should_ignore(rel):
            return

        # 같은 경로에 대기 중인 디바운스가 있으면 취소
        with self._timers_lock:
            pending = self._timers.pop(rel, None)
            if pending is not None:
                pending.cancel()

        self._mark_event()
        try:
            self._sync_engine.handle_local_change("deleted", rel)
        except Exception:
            logger.exception(f"삭제 이벤트 전파 실패: {rel}")

    def on_moved(self, event: Any) -> None:
        """이동을 delete+create로 분해한다.

        spec.md v2 P1 2-C 확정: on_moved를 delete(old_path) + create(new_path)로
        분해하여 version vector 증분을 정확하게 처리한다.
        old path에는 deleted=True + update(dev), new path에는 empty.update(dev).
        """
        if getattr(event, "is_directory", False):
            return

        src_rel = self._to_relative(getattr(event, "src_path", ""))
        dest_rel = self._to_relative(getattr(event, "dest_path", ""))
        if src_rel is None or dest_rel is None:
            return
        # 원본/대상 중 하나라도 유효 감시 대상이어야 한다
        if self._should_ignore(src_rel) and self._should_ignore(dest_rel):
            return

        self._mark_event()

        # delete old path (즉시, 디바운스 없음)
        if not self._should_ignore(src_rel):
            # 같은 경로에 대기 중인 디바운스가 있으면 취소
            with self._timers_lock:
                pending = self._timers.pop(src_rel, None)
                if pending is not None:
                    pending.cancel()
            try:
                self._sync_engine.handle_local_change("deleted", src_rel)
            except Exception:
                logger.exception(f"이동(삭제) 이벤트 전파 실패: {src_rel}")

        # create new path (디바운스 적용)
        if not self._should_ignore(dest_rel):
            self._enqueue_with_debounce_for_path(dest_rel, "created")

    # ── 디바운스 관리 ────────────────────────────────────────────────────

    def _enqueue_with_debounce(self, event: Any, event_type: str) -> None:
        """파일 경로별 디바운스 타이머를 (재)설정한다."""
        if getattr(event, "is_directory", False):
            return

        rel = self._to_relative(getattr(event, "src_path", ""))
        if rel is None or self._should_ignore(rel):
            return

        self._mark_event()
        self._enqueue_with_debounce_for_path(rel, event_type)

    def _enqueue_with_debounce_for_path(
        self, rel_path: str, event_type: str
    ) -> None:
        """상대 경로 기반 디바운스 타이머를 (재)설정한다."""
        with self._timers_lock:
            existing = self._timers.get(rel_path)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(
                self._debounce_seconds,
                self._fire_change,
                args=(rel_path, event_type),
            )
            timer.daemon = True
            self._timers[rel_path] = timer
            timer.start()

    def _fire_change(self, rel_path: str, event_type: str) -> None:
        """디바운스 만료 시 sync_engine에 단일 이벤트를 전달한다."""
        with self._timers_lock:
            self._timers.pop(rel_path, None)
        try:
            self._sync_engine.handle_local_change(event_type, rel_path)
        except Exception:
            logger.exception(f"디바운스 이벤트 전파 실패: {rel_path}")

    # ── 필터링 ───────────────────────────────────────────────────────────

    def _should_ignore(self, rel_path: str) -> bool:
        """config.should_ignore + .sync/ + 심볼릭 링크 필터."""
        if not rel_path:
            return True
        if should_ignore(rel_path):
            return True

        # .sync/ 이하 전부 무시 (IGNORE_PATTERNS가 이미 포함하지만 방어)
        first = rel_path.split("/", 1)[0]
        if first == SYNC_STATE_DIR:
            return True

        abs_path = self._vault_path / rel_path
        try:
            if abs_path.is_symlink():
                return True
        except OSError:
            pass
        return False

    def _to_relative(self, abs_path: str) -> str | None:
        """watchdog가 건네준 절대 경로를 볼트 기준 POSIX 상대 경로로 변환한다."""
        if not abs_path:
            return None
        try:
            rel = Path(abs_path).relative_to(self._vault_path)
        except ValueError:
            # 일부 OS(macOS)는 심볼릭 링크 해석 결과를 반환할 수 있음
            try:
                rel = Path(abs_path).resolve().relative_to(self._vault_path)
            except (ValueError, OSError):
                return None
        return rel.as_posix()

    # ── 활동성 ───────────────────────────────────────────────────────────

    def _mark_event(self) -> None:
        self._last_event_monotonic = time.monotonic()

    def last_event_age(self) -> float:
        """마지막 이벤트 이후 경과 초. 이벤트가 없으면 `inf`."""
        if self._last_event_monotonic is None:
            return float("inf")
        return time.monotonic() - self._last_event_monotonic

    # ── 테스트 유틸 ──────────────────────────────────────────────────────

    @property
    def pending_timer_count(self) -> int:
        """현재 보류 중인 디바운스 타이머 수 (테스트용)."""
        with self._timers_lock:
            return len(self._timers)

    @property
    def using_polling_fallback(self) -> bool:
        """PollingObserver fallback 상태."""
        return self._use_polling_fallback
