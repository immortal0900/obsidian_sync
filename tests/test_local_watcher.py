"""LocalWatcher 단위 테스트.

watchdog Observer 스레드를 직접 검증하기는 비결정적이므로,
이벤트 핸들러(on_created/on_modified/on_deleted/on_moved)를 직접 호출하고
디바운스 타이머가 만료될 시간을 sleep으로 확보하는 전략을 사용한다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.local_watcher import LocalWatcher


class FakeEvent:
    """watchdog 이벤트의 최소 프로토콜을 흉내낸다."""

    def __init__(
        self,
        src_path: str,
        *,
        dest_path: str | None = None,
        is_directory: bool = False,
    ) -> None:
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    (tmp_path / "notes").mkdir()
    return tmp_path


@pytest.fixture
def mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.handle_local_change = MagicMock()
    engine.execute = MagicMock()
    return engine


@pytest.fixture
def watcher(tmp_vault: Path, mock_engine: MagicMock) -> LocalWatcher:
    # 짧은 디바운스로 테스트 속도 확보
    return LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.05)


def _wait_debounce() -> None:
    # 디바운스(0.05s) + 여유 시간
    time.sleep(0.2)


class TestDebounce:
    """파일 경로별 디바운스 동작."""

    def test_single_file_five_rapid_modifications_collapse_to_one_call(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(str(tmp_vault / "a.md"))

        # 0.01초 간격으로 5회 수정 이벤트
        for _ in range(5):
            watcher.on_modified(event)
            time.sleep(0.01)

        _wait_debounce()

        assert mock_engine.handle_local_change.call_count == 1
        mock_engine.handle_local_change.assert_called_with("modified", "a.md")

    def test_ten_different_files_produce_ten_calls(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        for i in range(10):
            event = FakeEvent(str(tmp_vault / f"note{i}.md"))
            watcher.on_modified(event)

        _wait_debounce()

        assert mock_engine.handle_local_change.call_count == 10
        paths = {call.args[1] for call in mock_engine.handle_local_change.call_args_list}
        assert paths == {f"note{i}.md" for i in range(10)}

    def test_nested_path_debounces_independently(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        (tmp_vault / "folder" / "sub").mkdir(parents=True, exist_ok=True)

        watcher.on_modified(FakeEvent(str(tmp_vault / "folder" / "a.md")))
        watcher.on_modified(FakeEvent(str(tmp_vault / "folder" / "sub" / "b.md")))
        _wait_debounce()

        assert mock_engine.handle_local_change.call_count == 2
        calls = {call.args[1] for call in mock_engine.handle_local_change.call_args_list}
        assert calls == {"folder/a.md", "folder/sub/b.md"}


class TestImmediateDelete:
    def test_delete_bypasses_debounce(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(str(tmp_vault / "gone.md"))

        t0 = time.monotonic()
        watcher.on_deleted(event)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert mock_engine.handle_local_change.call_count == 1
        mock_engine.handle_local_change.assert_called_with("deleted", "gone.md")
        # 100ms 이내 전파
        assert elapsed_ms < 100

    def test_delete_cancels_pending_debounce(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(str(tmp_vault / "a.md"))
        watcher.on_modified(event)
        assert watcher.pending_timer_count == 1

        watcher.on_deleted(event)
        assert watcher.pending_timer_count == 0

        # 추가로 기다려도 수정 이벤트가 발사되지 않아야 한다
        _wait_debounce()
        calls = mock_engine.handle_local_change.call_args_list
        assert len(calls) == 1
        assert calls[0].args == ("deleted", "a.md")


class TestIgnorePatterns:
    @pytest.mark.parametrize(
        "rel_path",
        [
            ".obsidian/workspace.json",
            ".sync/sync_state.json",
            "foo.tmp",
            "bar.swp",
            ".DS_Store",
            "Thumbs.db",
            ".trash/old.md",
            ".git/config",
        ],
    )
    def test_ignored_paths_are_dropped(
        self,
        watcher: LocalWatcher,
        mock_engine: MagicMock,
        tmp_vault: Path,
        rel_path: str,
    ) -> None:
        event = FakeEvent(str(tmp_vault / rel_path))
        watcher.on_modified(event)
        _wait_debounce()
        mock_engine.handle_local_change.assert_not_called()

    def test_directory_events_ignored(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(str(tmp_vault / "folder"), is_directory=True)
        watcher.on_created(event)
        watcher.on_modified(event)
        watcher.on_deleted(event)
        _wait_debounce()
        mock_engine.handle_local_change.assert_not_called()

    def test_path_outside_vault_dropped(
        self,
        watcher: LocalWatcher,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        # tmp_vault와 다른 위치
        outside = tmp_path.parent / "other_dir"
        outside.mkdir(exist_ok=True)
        event = FakeEvent(str(outside / "file.md"))
        watcher.on_modified(event)
        _wait_debounce()
        mock_engine.handle_local_change.assert_not_called()

    @pytest.mark.skipif(
        os.name == "nt", reason="Windows에서는 심볼릭 링크 생성에 관리자 권한 필요"
    )
    def test_symlink_is_ignored(
        self,
        tmp_vault: Path,
        mock_engine: MagicMock,
    ) -> None:
        watcher = LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.05)
        real = tmp_vault / "real.md"
        real.write_text("x", encoding="utf-8")
        link = tmp_vault / "link.md"
        link.symlink_to(real)

        watcher.on_modified(FakeEvent(str(link)))
        _wait_debounce()
        mock_engine.handle_local_change.assert_not_called()


class TestMoved:
    def test_moved_debounces_and_emits_rename_remote(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(
            str(tmp_vault / "old.md"),
            dest_path=str(tmp_vault / "new.md"),
        )
        watcher.on_moved(event)
        _wait_debounce()

        mock_engine.execute.assert_called_once()
        action = mock_engine.execute.call_args.args[0]
        assert action["type"] == "rename_remote"
        assert action["old_path"] == "old.md"
        assert action["new_path"] == "new.md"

    def test_moved_same_dest_debounces(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        for _ in range(3):
            event = FakeEvent(
                str(tmp_vault / "old.md"),
                dest_path=str(tmp_vault / "new.md"),
            )
            watcher.on_moved(event)
            time.sleep(0.01)
        _wait_debounce()

        assert mock_engine.execute.call_count == 1

    def test_moved_directory_dropped(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        event = FakeEvent(
            str(tmp_vault / "src_dir"),
            dest_path=str(tmp_vault / "dst_dir"),
            is_directory=True,
        )
        watcher.on_moved(event)
        _wait_debounce()
        mock_engine.execute.assert_not_called()


class TestLastEventAge:
    def test_returns_inf_before_any_event(
        self, watcher: LocalWatcher
    ) -> None:
        assert watcher.last_event_age() == float("inf")

    def test_returns_finite_after_event(
        self, watcher: LocalWatcher, tmp_vault: Path
    ) -> None:
        watcher.on_modified(FakeEvent(str(tmp_vault / "a.md")))
        age = watcher.last_event_age()
        assert 0.0 <= age < 1.0


class TestStartStop:
    def test_stop_cancels_pending_timers(
        self, watcher: LocalWatcher, mock_engine: MagicMock, tmp_vault: Path
    ) -> None:
        for i in range(5):
            watcher.on_modified(FakeEvent(str(tmp_vault / f"n{i}.md")))

        assert watcher.pending_timer_count == 5

        watcher.stop()

        assert watcher.pending_timer_count == 0
        # 취소 후 추가 이벤트가 발사되지 않음을 확인
        time.sleep(0.2)
        mock_engine.handle_local_change.assert_not_called()

    def test_start_stop_with_real_observer(
        self, tmp_vault: Path, mock_engine: MagicMock
    ) -> None:
        watcher = LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.05)
        watcher.start()

        assert watcher._observer is not None

        watcher.stop()

        assert watcher._observer is None
        # 옵저버 스레드가 종료되었는지는 stop() 내 join으로 보장됨

    def test_start_file_event_end_to_end(
        self, tmp_vault: Path, mock_engine: MagicMock
    ) -> None:
        """watchdog이 실제 파일 이벤트를 잡아 sync_engine까지 도달하는지 종단 검증."""
        watcher = LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.1)
        watcher.start()
        try:
            target = tmp_vault / "end2end.md"
            target.write_text("hello", encoding="utf-8")
            # OS 이벤트 전달 + 디바운스 만료 대기
            deadline = time.monotonic() + 3.0
            while (
                mock_engine.handle_local_change.call_count == 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
        finally:
            watcher.stop()

        assert mock_engine.handle_local_change.call_count >= 1
        paths = {
            call.args[1] for call in mock_engine.handle_local_change.call_args_list
        }
        assert "end2end.md" in paths


class TestHelpers:
    def test_to_relative_drops_symbolic_fallback(
        self,
        tmp_vault: Path,
        mock_engine: MagicMock,
    ) -> None:
        watcher = LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.05)
        assert watcher._to_relative("") is None
        assert watcher._to_relative(str(tmp_vault / "a.md")) == "a.md"

    def test_should_ignore_covers_sync_dir(
        self,
        tmp_vault: Path,
        mock_engine: MagicMock,
    ) -> None:
        watcher = LocalWatcher(tmp_vault, mock_engine, debounce_seconds=0.05)
        assert watcher._should_ignore(".sync/anything.json") is True
        assert watcher._should_ignore("note.md") is False

    def test_engine_exception_does_not_propagate(
        self,
        tmp_vault: Path,
    ) -> None:
        engine = SimpleNamespace(
            handle_local_change=MagicMock(side_effect=RuntimeError("boom")),
            execute=MagicMock(),
        )
        watcher = LocalWatcher(tmp_vault, engine, debounce_seconds=0.05)
        # 예외가 상위로 올라오지 않아야 한다
        watcher.on_deleted(FakeEvent(str(tmp_vault / "x.md")))
        # 디바운스 경로에서도 상위 전파되지 않음
        watcher.on_modified(FakeEvent(str(tmp_vault / "y.md")))
        _wait_debounce()
