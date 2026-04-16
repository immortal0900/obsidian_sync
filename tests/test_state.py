"""src/state.py 단위 테스트."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.config import SyncConfig
from src.state import FileEntry, SyncState


@pytest.fixture
def mock_config(tmp_path):
    """테스트용 SyncConfig를 생성한다."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return SyncConfig(
        vault_path=vault_dir,
        drive_folder_id="test_folder_id",
        device_id="test_pc",
        credentials_file=Path("credentials.json"),
        token_file=Path("token.json"),
    )


@pytest.fixture
def sync_state(mock_config):
    """테스트용 SyncState를 생성한다."""
    return SyncState(mock_config)


@pytest.fixture
def populated_vault(mock_config):
    """파일이 있는 볼트 디렉토리를 생성한다."""
    vault = mock_config.vault_path

    # 일반 파일
    (vault / "note1.md").write_text("hello", encoding="utf-8")
    (vault / "daily").mkdir()
    (vault / "daily" / "2026-04-14.md").write_text("daily note", encoding="utf-8")

    # 한국어 파일명
    (vault / "메모").mkdir()
    (vault / "메모" / "중요.md").write_text("중요한 내용", encoding="utf-8")

    # 제외 대상
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (vault / ".sync").mkdir()
    (vault / ".sync" / "sync_state.json").write_text("{}", encoding="utf-8")
    (vault / "temp.tmp").write_text("temp", encoding="utf-8")

    return mock_config


class TestFileEntry:
    """FileEntry 데이터클래스 테스트."""

    def test_to_dict(self):
        entry = FileEntry(mtime=1000.0, size=2048, drive_id="abc123")
        d = entry.to_dict()
        assert d == {"mtime": 1000.0, "size": 2048, "drive_id": "abc123"}

    def test_to_dict_without_drive_id(self):
        entry = FileEntry(mtime=1000.0, size=2048)
        d = entry.to_dict()
        assert d == {"mtime": 1000.0, "size": 2048}
        assert "drive_id" not in d

    def test_from_dict(self):
        data = {"mtime": 1000.0, "size": 2048, "drive_id": "abc123"}
        entry = FileEntry.from_dict(data)
        assert entry.mtime == 1000.0
        assert entry.size == 2048
        assert entry.drive_id == "abc123"

    def test_from_dict_without_drive_id(self):
        data = {"mtime": 1000.0, "size": 2048}
        entry = FileEntry.from_dict(data)
        assert entry.drive_id is None


class TestSyncStateLoad:
    """SyncState.load 테스트."""

    def test_load_missing_file(self, sync_state):
        """상태 파일이 없으면 False."""
        assert sync_state.load() is False

    def test_load_valid_state(self, sync_state):
        """유효한 상태 파일을 정상 로드한다."""
        state_dir = sync_state._state_dir
        state_dir.mkdir(parents=True)

        state_data = {
            "version": 1,
            "device_id": "my_pc",
            "page_token": "12345",
            "last_synced_at": 1713100000.0,
            "files": {
                "note.md": {"mtime": 1000.0, "size": 100, "drive_id": "id1"},
                "daily/today.md": {"mtime": 2000.0, "size": 200},
            },
        }
        sync_state._state_file.write_text(
            json.dumps(state_data, ensure_ascii=False), encoding="utf-8"
        )

        assert sync_state.load() is True
        assert sync_state.device_id == "my_pc"
        assert sync_state.page_token == "12345"
        assert sync_state.last_synced_at == 1713100000.0
        assert len(sync_state.files) == 2
        assert sync_state.files["note.md"].drive_id == "id1"
        assert sync_state.files["daily/today.md"].drive_id is None

    def test_load_corrupt_json_creates_backup(self, sync_state):
        """JSON 파싱 실패 시 .backup 파일을 생성하고 False를 반환한다."""
        state_dir = sync_state._state_dir
        state_dir.mkdir(parents=True)
        sync_state._state_file.write_text("INVALID JSON{{{", encoding="utf-8")

        assert sync_state.load() is False

        backup_path = sync_state._state_file.with_suffix(".json.backup")
        assert backup_path.exists()
        assert not sync_state._state_file.exists()

    def test_load_version_mismatch_returns_false(self, sync_state):
        """version 불일치 시 False 반환 + 백업 생성 → run_without_state 경로 유도."""
        state_dir = sync_state._state_dir
        state_dir.mkdir(parents=True)

        future_state = {
            "version": 2,
            "device_id": "my_pc",
            "page_token": "x",
            "last_synced_at": 0,
            "files": {},
        }
        sync_state._state_file.write_text(
            json.dumps(future_state), encoding="utf-8"
        )

        assert sync_state.load() is False
        backup_path = sync_state._state_file.with_suffix(".json.backup")
        assert backup_path.exists()
        assert not sync_state._state_file.exists()


class TestSyncStateSave:
    """SyncState.save 테스트."""

    def test_save_immediate(self, sync_state):
        """immediate=True일 때 즉시 파일이 생성된다."""
        sync_state.files["test.md"] = FileEntry(mtime=1000.0, size=100, drive_id="id1")
        sync_state.page_token = "99999"
        sync_state.save(immediate=True)

        assert sync_state._state_file.exists()
        data = json.loads(sync_state._state_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["page_token"] == "99999"
        assert data["files"]["test.md"]["mtime"] == 1000.0
        assert data["files"]["test.md"]["drive_id"] == "id1"

    def test_save_creates_sync_dir(self, sync_state):
        """저장 시 .sync/ 디렉토리를 자동 생성한다."""
        assert not sync_state._state_dir.exists()
        sync_state.save(immediate=True)
        assert sync_state._state_dir.exists()

    def test_save_korean_filenames(self, sync_state):
        """한국어 파일명이 ensure_ascii=False로 저장된다."""
        sync_state.files["메모/중요.md"] = FileEntry(mtime=1000.0, size=100)
        sync_state.save(immediate=True)

        raw = sync_state._state_file.read_text(encoding="utf-8")
        assert "메모/중요.md" in raw  # 이스케이프 없이 저장

    def test_save_debounced(self, sync_state):
        """immediate=False일 때 타이머가 설정된다."""
        sync_state.files["test.md"] = FileEntry(mtime=1000.0, size=100)
        sync_state.save(immediate=False)

        # 타이머가 설정되었는지 확인
        assert sync_state._save_timer is not None
        assert sync_state._save_timer.is_alive()

        # 타이머 취소 (테스트 정리)
        sync_state._save_timer.cancel()


class TestSyncStateScanLocalFiles:
    """SyncState.scan_local_files 테스트."""

    def test_scan_finds_normal_files(self, populated_vault):
        """일반 파일을 정상적으로 스캔한다."""
        state = SyncState(populated_vault)
        files = state.scan_local_files()

        assert "note1.md" in files
        assert "daily/2026-04-14.md" in files

    def test_scan_finds_korean_filenames(self, populated_vault):
        """한국어 파일명도 정상 스캔한다."""
        state = SyncState(populated_vault)
        files = state.scan_local_files()

        # 한국어 경로 확인 (POSIX 스타일)
        korean_files = [k for k in files if "메모" in k]
        assert len(korean_files) == 1

    def test_scan_ignores_excluded(self, populated_vault):
        """제외 대상 파일은 스캔하지 않는다."""
        state = SyncState(populated_vault)
        files = state.scan_local_files()

        # 제외 대상이 포함되지 않았는지 확인
        for path in files:
            assert ".obsidian" not in path
            assert ".sync" not in path
            assert not path.endswith(".tmp")

    def test_scan_preserves_drive_id(self, populated_vault):
        """기존 files에서 drive_id를 복사한다."""
        state = SyncState(populated_vault)
        state.files["note1.md"] = FileEntry(mtime=0, size=0, drive_id="existing_id")

        files = state.scan_local_files()
        assert files["note1.md"].drive_id == "existing_id"

    def test_scan_has_mtime_and_size(self, populated_vault):
        """스캔 결과에 mtime과 size가 포함된다."""
        state = SyncState(populated_vault)
        files = state.scan_local_files()

        entry = files["note1.md"]
        assert entry.mtime > 0
        assert entry.size > 0


class TestSyncStateDiff:
    """SyncState.diff 정적 메서드 테스트."""

    def test_added_files(self):
        old = {}
        new = {"a.md": FileEntry(1000.0, 100), "b.md": FileEntry(2000.0, 200)}
        result = SyncState.diff(old, new)
        assert result.added == ["a.md", "b.md"]
        assert result.modified == []
        assert result.deleted == []

    def test_deleted_files(self):
        old = {"a.md": FileEntry(1000.0, 100), "b.md": FileEntry(2000.0, 200)}
        new = {}
        result = SyncState.diff(old, new)
        assert result.added == []
        assert result.modified == []
        assert result.deleted == ["a.md", "b.md"]

    def test_modified_by_mtime(self):
        old = {"a.md": FileEntry(1000.0, 100)}
        new = {"a.md": FileEntry(2000.0, 100)}  # mtime 변경
        result = SyncState.diff(old, new)
        assert result.modified == ["a.md"]

    def test_modified_by_size(self):
        old = {"a.md": FileEntry(1000.0, 100)}
        new = {"a.md": FileEntry(1000.0, 200)}  # size 변경
        result = SyncState.diff(old, new)
        assert result.modified == ["a.md"]

    def test_unchanged_files(self):
        old = {"a.md": FileEntry(1000.0, 100)}
        new = {"a.md": FileEntry(1000.0, 100)}
        result = SyncState.diff(old, new)
        assert result.added == []
        assert result.modified == []
        assert result.deleted == []

    def test_mixed_changes(self):
        old = {
            "keep.md": FileEntry(1000.0, 100),
            "modify.md": FileEntry(1000.0, 100),
            "delete.md": FileEntry(1000.0, 100),
        }
        new = {
            "keep.md": FileEntry(1000.0, 100),
            "modify.md": FileEntry(2000.0, 200),
            "add.md": FileEntry(3000.0, 300),
        }
        result = SyncState.diff(old, new)
        assert result.added == ["add.md"]
        assert result.modified == ["modify.md"]
        assert result.deleted == ["delete.md"]

    def test_results_are_sorted(self):
        old = {}
        new = {
            "c.md": FileEntry(1.0, 1),
            "a.md": FileEntry(1.0, 1),
            "b.md": FileEntry(1.0, 1),
        }
        result = SyncState.diff(old, new)
        assert result.added == ["a.md", "b.md", "c.md"]


class TestSyncStateUpdateRemove:
    """SyncState.update_file / remove_file 테스트."""

    def test_update_file(self, sync_state):
        """update_file이 인메모리 상태를 갱신한다."""
        entry = FileEntry(mtime=1000.0, size=100, drive_id="id1")
        sync_state.update_file("test.md", entry)
        assert "test.md" in sync_state.files
        assert sync_state.files["test.md"].drive_id == "id1"

        # 타이머 정리
        if sync_state._save_timer:
            sync_state._save_timer.cancel()

    def test_remove_file(self, sync_state):
        """remove_file이 인메모리 상태에서 항목을 삭제한다."""
        sync_state.files["test.md"] = FileEntry(mtime=1000.0, size=100)
        sync_state.remove_file("test.md")
        assert "test.md" not in sync_state.files

        # 타이머 정리
        if sync_state._save_timer:
            sync_state._save_timer.cancel()

    def test_remove_nonexistent_file(self, sync_state):
        """존재하지 않는 파일 삭제 시 에러가 발생하지 않는다."""
        sync_state.remove_file("nonexistent.md")  # 에러 없이 통과

        # 타이머 정리
        if sync_state._save_timer:
            sync_state._save_timer.cancel()


class TestSyncStateAtomicWrite:
    """save() atomic write 검증."""

    def test_existing_file_preserved_on_write_failure(self, sync_state):
        """_write_state_file 중간 예외 발생 시 기존 파일이 온전히 보존된다."""
        from unittest.mock import patch

        # 1회차 저장으로 온전한 상태 파일 생성
        sync_state.files["keep.md"] = FileEntry(mtime=111.0, size=11, drive_id="id1")
        sync_state.save(immediate=True)
        original_bytes = sync_state._state_file.read_bytes()

        # 2회차: os.replace 실패 모의 → 기존 파일 그대로 유지 + tmp 정리
        sync_state.files["keep.md"] = FileEntry(mtime=222.0, size=22, drive_id="id2")
        with patch(
            "src.state.os.replace", side_effect=OSError("simulated disk failure")
        ):
            sync_state.save(immediate=True)  # 내부에서 실패 로깅

        # 기존 파일이 그대로 남아있어야 한다
        assert sync_state._state_file.read_bytes() == original_bytes

        # tmp 파일이 남지 않았는지 확인
        leftover = list(sync_state._state_dir.glob("*.tmp"))
        assert leftover == []


class TestSyncStateSize:
    """대용량 상태 파일 크기 검증."""

    def test_1000_files_size_under_150kb(self, sync_state):
        """1,000개 파일 상태 파일 크기가 150KB 이하."""
        for i in range(1000):
            path = f"notes/section_{i // 50}/file_{i:04d}.md"
            sync_state.files[path] = FileEntry(
                mtime=1713000000.0 + i,
                size=2048 + i,
                drive_id=f"drive_id_{i:020d}",
            )
        sync_state.page_token = "999999"
        sync_state.save(immediate=True)

        size_bytes = sync_state._state_file.stat().st_size
        assert size_bytes <= 150 * 1024, f"크기 초과: {size_bytes} bytes"


class TestSyncStatePathNormalization:
    """Windows 경로 POSIX 정규화 검증."""

    def test_scan_normalizes_backslash_to_forward_slash(self, mock_config):
        """scan_local_files 결과 key는 항상 POSIX 구분자를 사용한다."""
        vault = mock_config.vault_path
        (vault / "daily").mkdir()
        (vault / "daily" / "2026-04-14.md").write_text("x", encoding="utf-8")

        state = SyncState(mock_config)
        files = state.scan_local_files()

        # 결과 key는 항상 "/" 사용
        assert "daily/2026-04-14.md" in files
        for key in files:
            assert "\\" not in key

    def test_saved_state_has_posix_keys(self, mock_config):
        """save() 결과 JSON의 files 키도 POSIX 구분자를 유지한다."""
        state = SyncState(mock_config)
        state.files["sub/dir/note.md"] = FileEntry(mtime=1.0, size=1)
        state.save(immediate=True)

        raw = state._state_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        for key in data["files"]:
            assert "\\" not in key


class TestSyncStateShutdown:
    """SyncState.shutdown 테스트."""

    def test_shutdown_saves_immediately(self, sync_state):
        """shutdown이 즉시 저장하고 last_synced_at을 갱신한다."""
        sync_state.files["test.md"] = FileEntry(mtime=1000.0, size=100)
        before = time.time()
        sync_state.shutdown()
        after = time.time()

        assert sync_state._state_file.exists()
        assert sync_state.last_synced_at is not None
        assert before <= sync_state.last_synced_at <= after

    def test_shutdown_cancels_pending_timer(self, sync_state):
        """shutdown이 보류 중인 타이머를 취소한다."""
        sync_state.save(immediate=False)
        assert sync_state._save_timer is not None

        sync_state.shutdown()
        assert sync_state._save_timer is None
