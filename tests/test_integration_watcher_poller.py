"""LocalWatcher × AdaptivePoller × SyncEngine 통합 회귀 테스트.

실제 임시 볼트 + mock DriveClient 조합으로 종단 시나리오 4건을 검증한다.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import STATE_SAVE_DEBOUNCE_SECONDS, SyncConfig
from src.conflict import ConflictResolver
from src.local_watcher import LocalWatcher
from src.poller import AdaptivePoller
from src.state import FileEntry, SyncState
from src.sync_engine import SyncEngine

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / ".sync").mkdir()
    return tmp_path


@pytest.fixture
def config(vault: Path) -> SyncConfig:
    return SyncConfig(
        vault_path=vault,
        drive_folder_id="root_id",
        device_id="test_pc",
        credentials_file=vault / "credentials.json",
        token_file=vault / "token.json",
    )


@pytest.fixture
def state(config: SyncConfig) -> SyncState:
    return SyncState(config)


@pytest.fixture
def resolver(vault: Path) -> ConflictResolver:
    return ConflictResolver(device_id="test_pc", vault_path=vault)


@pytest.fixture
def drive() -> MagicMock:
    m = MagicMock()
    counter = {"n": 0}

    def _upload(
        local: Path,
        rel: str,
        existing_id: str | None = None,
        *,
        app_properties: dict | None = None,
    ) -> dict:
        counter["n"] += 1
        fid = existing_id or f"drive_id_{counter['n']}"
        return {"id": fid, "md5Checksum": None, "appProperties": app_properties or {}}

    m.upload.side_effect = _upload

    def _download(file_id: str, local_path: Path) -> dict:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"remote content")
        return {"id": file_id, "md5Checksum": None, "appProperties": {}}

    m.download.side_effect = _download
    m.hard_delete = MagicMock()
    m.move_to_tombstones = MagicMock()
    m.rename = MagicMock()
    return m


@pytest.fixture
def engine(
    drive: MagicMock, state: SyncState, resolver: ConflictResolver
) -> SyncEngine:
    return SyncEngine(drive, state, resolver)


def _fake_event(src: str, *, dest: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(src_path=src, dest_path=dest, is_directory=False)


# ── 시나리오 (a) ────────────────────────────────────────────────────────


class TestScenarioA:
    """로컬 파일 3개 (nested 포함) 빠른 수정 → upload 3건 + state 반영."""

    def test_three_files_with_nested_path_result_in_three_uploads(
        self,
        vault: Path,
        engine: SyncEngine,
        state: SyncState,
        drive: MagicMock,
    ) -> None:
        files = ["a.md", "folder/b.md", "foo/bar/note.md"]
        for rel in files:
            p = vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content", encoding="utf-8")

        watcher = LocalWatcher(vault, engine, debounce_seconds=0.05)

        # 각 파일에 대해 5회 연속 이벤트
        for rel in files:
            for _ in range(5):
                watcher.on_modified(_fake_event(str(vault / rel)))
                time.sleep(0.005)

        # 디바운스 만료 대기
        time.sleep(0.3)

        # 파일당 1회씩 = 3회 upload
        assert drive.upload.call_count == 3

        for rel in files:
            assert rel in state.files
            assert state.files[rel].drive_id is not None

        # nested 경로의 upload는 정상 호출됨
        nested_call = [
            c for c in drive.upload.call_args_list
            if c.args[1] == "foo/bar/note.md"
        ]
        assert len(nested_call) == 1


# ── 시나리오 (b) ────────────────────────────────────────────────────────


class TestScenarioB:
    """mock get_changes 1건 → poller 1회 → download + state 갱신."""

    async def test_single_remote_change_downloads_and_updates_token(
        self,
        vault: Path,
        engine: SyncEngine,
        state: SyncState,
        drive: MagicMock,
    ) -> None:
        state.page_token = "T0"
        state.files["remote.md"] = FileEntry(mtime=0.0, size=0, drive_id="rid")

        changes = [
            {
                "file_id": "rid",
                "removed": False,
                "file": {
                    "name": "remote.md",
                    "modified_time": "2026-04-14T10:00:00Z",
                    "md5": "m",
                },
            }
        ]
        drive.get_changes.return_value = (changes, "T1")

        watcher = LocalWatcher(vault, engine, debounce_seconds=0.05)
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=watcher,
            state=state,
        )

        await poller.poll_once()

        drive.download.assert_called_once()
        assert (vault / "remote.md").read_bytes() == b"remote content"
        assert state.page_token == "T1"
        # 원격 변경 수신 직후 다음 폴링은 최소 간격으로 리셋됨
        assert poller.current_interval == 10.0


# ── 시나리오 (c) ────────────────────────────────────────────────────────


class TestScenarioC:
    """watcher + poller 교차 이벤트 → SyncEngine.lock 큐잉으로 손실 0."""

    def test_crossing_events_all_processed(
        self,
        vault: Path,
        engine: SyncEngine,
        state: SyncState,
        drive: MagicMock,
    ) -> None:
        # 로컬에만 있는 파일 (watcher가 처리)
        (vault / "local.md").write_text("local", encoding="utf-8")
        # 원격에 이미 매핑된 파일 (poller가 처리)
        state.files["remote.md"] = FileEntry(mtime=0.0, size=0, drive_id="rid_remote")

        watcher = LocalWatcher(vault, engine, debounce_seconds=0.05)

        # 1) watcher 이벤트 (디바운스 후 upload)
        watcher.on_modified(_fake_event(str(vault / "local.md")))

        # 2) poller 이벤트 (즉시 execute 경로)
        remote_changes = [
            {
                "file_id": "rid_remote",
                "removed": False,
                "file": {
                    "name": "remote.md",
                    "modified_time": "t",
                    "md5": "m",
                },
            }
        ]
        engine.handle_remote_changes(remote_changes)

        # 3) 디바운스 대기 후 watcher의 upload까지 완료
        time.sleep(0.3)

        assert drive.upload.call_count == 1
        assert drive.download.call_count == 1
        assert state.files["local.md"].drive_id is not None
        # poller 경로의 download도 state 반영
        assert "remote.md" in state.files
        assert (vault / "remote.md").read_bytes() == b"remote content"
        assert engine.lock is False  # 잠금 해제된 상태
        watcher.stop()


# ── 시나리오 (d) ────────────────────────────────────────────────────────


class TestScenarioD:
    """state.save() 디바운스 (5초) + stop() 시 즉시 flush."""

    def test_state_save_immediate_writes_file(
        self,
        config: SyncConfig,
        state: SyncState,
    ) -> None:
        state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="id")
        state.save(immediate=True)
        assert config.state_file.exists()

    def test_state_save_debounced_does_not_write_immediately(
        self,
        config: SyncConfig,
        state: SyncState,
    ) -> None:
        # 디바운스는 5초 — 호출 직후엔 파일이 만들어지지 않아야 한다
        state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="id")
        state.save(immediate=False)

        time.sleep(0.1)  # 디바운스 만료 전
        assert not config.state_file.exists()

        # 타이머 취소 (leak 방지)
        if state._save_timer is not None:
            state._save_timer.cancel()

    def test_debounce_coalesces_rapid_saves(
        self,
        config: SyncConfig,
        state: SyncState,
    ) -> None:
        """빠르게 여러 번 save(immediate=False) 호출해도 단일 타이머로 묶인다."""
        call_counter = {"n": 0}
        original = state._write_state_file

        def tracked() -> None:
            call_counter["n"] += 1
            original()

        state._write_state_file = tracked  # type: ignore[method-assign]

        for _ in range(100):
            state.save(immediate=False)

        time.sleep(0.1)
        # 디바운스로 인해 아직 발화하지 않음 (5초 후 1회 예정)
        assert call_counter["n"] == 0

        # 타이머 취소
        if state._save_timer is not None:
            state._save_timer.cancel()

    def test_shutdown_flushes_pending_save(
        self,
        config: SyncConfig,
        state: SyncState,
    ) -> None:
        state.files["flush.md"] = FileEntry(mtime=1.0, size=1, drive_id="id")
        state.save(immediate=False)  # 디바운스 예약
        assert not config.state_file.exists()

        state.shutdown()

        # shutdown 후 파일 존재 + last_synced_at 기록됨
        assert config.state_file.exists()
        assert state.last_synced_at is not None

    def test_debounce_constant_is_five_seconds(self) -> None:
        """회귀 방지: 디바운스 상수가 5초 유지되는지 확인."""
        assert STATE_SAVE_DEBOUNCE_SECONDS == 5.0


# ── 통합 smoke: watcher + poller + engine 함께 동작 ─────────────────────


class TestCoarseIntegrationSmoke:
    async def test_watcher_then_poller_end_to_end(
        self,
        vault: Path,
        engine: SyncEngine,
        state: SyncState,
        drive: MagicMock,
    ) -> None:
        state.page_token = "T0"
        drive.get_changes.return_value = ([], "T1")

        watcher = LocalWatcher(vault, engine, debounce_seconds=0.05)
        poller = AdaptivePoller(
            drive_client=drive,
            sync_engine=engine,
            local_watcher=watcher,
            state=state,
        )

        # 로컬 이벤트
        (vault / "local.md").write_text("x", encoding="utf-8")
        watcher.on_created(_fake_event(str(vault / "local.md")))
        time.sleep(0.2)

        # 폴링 1회 (빈 변경)
        await poller.poll_once()

        assert drive.upload.call_count == 1
        assert state.page_token == "T1"
        assert "local.md" in state.files
        watcher.stop()
