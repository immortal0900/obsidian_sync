"""sync_engine.py 테스트.

action 디스패처, 재진입 방지 잠금, state 갱신, 원격 변경 → action 매핑을 검증한다.
DriveClient는 MagicMock으로 대체한다.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import SyncConfig
from src.conflict import ConflictResolver
from src.drive_client import DriveFileNotFoundError
from src.state import FileEntry, SyncState
from src.sync_engine import (
    ACTION_CONFLICT,
    ACTION_DELETE_LOCAL,
    ACTION_DELETE_REMOTE,
    ACTION_DOWNLOAD,
    ACTION_RENAME_REMOTE,
    ACTION_UPLOAD,
    SyncEngine,
)

# ── fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / ".sync").mkdir()
    return tmp_path


@pytest.fixture
def config(vault: Path) -> SyncConfig:
    return SyncConfig(
        vault_path=vault,
        drive_folder_id="root_id",
        device_id="my_pc",
        credentials_file=vault / "credentials.json",
        token_file=vault / "token.json",
    )


@pytest.fixture
def state(config: SyncConfig) -> SyncState:
    s = SyncState(config)
    return s


@pytest.fixture
def drive() -> MagicMock:
    mock = MagicMock()
    mock.upload.return_value = {"id": "drive_id_new", "md5Checksum": "drive_md5_hash"}
    return mock


@pytest.fixture
def resolver(vault: Path) -> ConflictResolver:
    return ConflictResolver(device_id="my_pc", vault_path=vault)


@pytest.fixture
def engine(drive: MagicMock, state: SyncState, resolver: ConflictResolver) -> SyncEngine:
    return SyncEngine(drive, state, resolver)


def _write(vault: Path, rel: str, content: bytes = b"x") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


# ── upload ──────────────────────────────────────────────────────────────


def test_upload_creates_file_entry(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    _write(vault, "daily/note.md", b"content")
    engine.execute({"type": ACTION_UPLOAD, "path": "daily/note.md"})

    drive.upload.assert_called_once()
    args, kwargs = drive.upload.call_args
    assert kwargs.get("existing_id") is None
    assert "daily/note.md" in state.files
    assert state.files["daily/note.md"].drive_id == "drive_id_new"
    assert state.files["daily/note.md"].size == len(b"content")
    assert state.files["daily/note.md"].md5 == "drive_md5_hash"


def test_upload_updates_existing_drive_id(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    """기존 drive_id가 있으면 update 경로로 재사용한다."""
    _write(vault, "note.md", b"v2")
    state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="existing_id")
    drive.upload.return_value = {"id": "existing_id", "md5Checksum": "updated_md5"}

    engine.execute({"type": ACTION_UPLOAD, "path": "note.md"})

    args, kwargs = drive.upload.call_args
    assert kwargs.get("existing_id") == "existing_id"
    assert state.files["note.md"].drive_id == "existing_id"


def test_upload_missing_file_is_noop(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    engine.execute({"type": ACTION_UPLOAD, "path": "gone.md"})
    drive.upload.assert_not_called()
    assert "gone.md" not in state.files


# ── download ────────────────────────────────────────────────────────────


def test_download_writes_state_entry(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    def _fake_download(file_id: str, local_path: Path) -> dict:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"remote")
        return {"id": file_id, "md5Checksum": "abc123", "appProperties": {}}

    drive.download.side_effect = _fake_download

    engine.execute(
        {"type": ACTION_DOWNLOAD, "file_id": "rid", "path": "notes/a.md"}
    )

    assert (vault / "notes/a.md").read_bytes() == b"remote"
    assert state.files["notes/a.md"].drive_id == "rid"
    assert state.files["notes/a.md"].size == len(b"remote")
    assert state.files["notes/a.md"].md5 == "abc123"


def test_download_applies_remote_version_vector(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    """download 후 로컬 version이 원격 appProperties의 vector로 설정된다."""

    def _fake_download(file_id: str, local_path: Path) -> dict:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"remote data")
        return {
            "id": file_id,
            "md5Checksum": "remote_md5",
            "appProperties": {
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_a1b2c3d4": "1745000000123",
            },
        }

    drive.download.side_effect = _fake_download

    engine.execute(
        {"type": ACTION_DOWNLOAD, "file_id": "rid2", "path": "remote.md"}
    )

    entry = state.files["remote.md"]
    assert entry.version.counters == {"a1b2c3d4": 1745000000123}
    assert entry.md5 == "remote_md5"


def test_download_handles_none_md5_gracefully(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    """Google Docs 등 md5Checksum이 None인 파일도 정상 처리."""

    def _fake_download(file_id: str, local_path: Path) -> dict:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"google doc")
        return {"id": file_id, "md5Checksum": None, "appProperties": {}}

    drive.download.side_effect = _fake_download

    engine.execute(
        {"type": ACTION_DOWNLOAD, "file_id": "gdoc", "path": "doc.md"}
    )

    entry = state.files["doc.md"]
    # Drive md5 is None (Google Docs), but local compute_md5 fills it
    assert entry.md5 is not None  # local md5 computed
    assert entry.drive_id == "gdoc"


# ── delete ──────────────────────────────────────────────────────────────


def test_delete_remote_marks_deleted_in_state(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")
    engine.execute(
        {"type": ACTION_DELETE_REMOTE, "file_id": "did", "path": "note.md"}
    )
    drive.move_to_tombstones.assert_called_once()
    call_args = drive.move_to_tombstones.call_args
    assert call_args[0][0] == "did"  # file_id
    # v2: deleted=True + version 갱신으로 tombstone 기록
    assert "note.md" in state.files
    assert state.files["note.md"].deleted is True
    assert state.files["note.md"].version.counters  # version이 갱신됨


def test_delete_local_removes_file_and_marks_deleted(
    engine: SyncEngine, state: SyncState, vault: Path
) -> None:
    p = _write(vault, "note.md", b"x")
    state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")
    engine.execute({"type": ACTION_DELETE_LOCAL, "path": "note.md"})
    assert not p.exists()
    # v2: deleted=True + version 갱신으로 tombstone 기록
    assert "note.md" in state.files
    assert state.files["note.md"].deleted is True


def test_delete_local_missing_file_still_marks_deleted(
    engine: SyncEngine, state: SyncState
) -> None:
    state.files["ghost.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")
    engine.execute({"type": ACTION_DELETE_LOCAL, "path": "ghost.md"})
    # v2: deleted=True로 마킹 (tombstone)
    assert "ghost.md" in state.files
    assert state.files["ghost.md"].deleted is True


# ── rename ──────────────────────────────────────────────────────────────


def test_rename_remote_updates_state_key(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    state.files["old.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")
    engine.execute(
        {
            "type": ACTION_RENAME_REMOTE,
            "old_path": "old.md",
            "new_path": "new.md",
        }
    )
    drive.rename.assert_called_once_with("did", "new.md")
    assert "old.md" not in state.files
    assert state.files["new.md"].drive_id == "did"


def test_rename_remote_ignored_when_not_in_state(
    engine: SyncEngine, drive: MagicMock
) -> None:
    engine.execute(
        {"type": ACTION_RENAME_REMOTE, "old_path": "x.md", "new_path": "y.md"}
    )
    drive.rename.assert_not_called()


# ── conflict ────────────────────────────────────────────────────────────


def test_conflict_creates_copy_and_downloads_remote(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    _write(vault, "c.md", b"local")

    def _download(file_id: str, local_path: Path) -> None:
        local_path.write_bytes(b"remote_version")

    drive.download.side_effect = _download

    engine.execute(
        {
            "type": ACTION_CONFLICT,
            "path": "c.md",
            "local": {"mtime": 1.0, "size": 5},
            "remote": {"file_id": "rid", "modified_time": "2026"},
        }
    )

    # 원본은 클라우드 버전으로 덮어씀
    assert (vault / "c.md").read_bytes() == b"remote_version"
    # 충돌 사본 하나 생성됨
    conflicts = [
        p for p in vault.iterdir() if ".sync-conflict-" in p.name
    ]
    assert len(conflicts) == 1
    # state도 rid로 갱신
    assert state.files["c.md"].drive_id == "rid"


# ── 잠금 동작 ────────────────────────────────────────────────────────────


def test_reentry_queues_action(
    state: SyncState, drive: MagicMock, resolver: ConflictResolver, vault: Path
) -> None:
    """_do_upload 실행 중 추가 execute 호출 시 큐에 쌓이고 순차 처리된다."""
    _write(vault, "a.md", b"a")
    _write(vault, "b.md", b"b")

    engine = SyncEngine(drive, state, resolver)

    call_order: list[str] = []

    def _upload_side_effect(local_path: Path, rel_path: str, **_: object) -> str:
        call_order.append(rel_path)
        if rel_path == "a.md":
            # 재진입 시도: 잠금 보유 중이므로 큐에 쌓여야 함
            engine.execute({"type": ACTION_UPLOAD, "path": "b.md"})
            # 이 시점에 b.md는 실제로 업로드되면 안 됨
            assert "b.md" not in call_order
        return f"id_{rel_path}"

    drive.upload.side_effect = _upload_side_effect

    engine.execute({"type": ACTION_UPLOAD, "path": "a.md"})

    # 최종 호출 순서: a.md → b.md
    assert call_order == ["a.md", "b.md"]
    assert engine.lock is False  # 해제됨
    assert state.files["a.md"].drive_id == "id_a.md"
    assert state.files["b.md"].drive_id == "id_b.md"


def test_lock_released_on_exception(
    engine: SyncEngine, drive: MagicMock, vault: Path
) -> None:
    """내부 예외가 발생해도 잠금은 해제된다 (log는 남음)."""
    _write(vault, "a.md", b"x")
    drive.upload.side_effect = RuntimeError("boom")

    engine.execute({"type": ACTION_UPLOAD, "path": "a.md"})
    assert engine.lock is False


# ── handle_local_change ─────────────────────────────────────────────────


def test_handle_local_change_modified_triggers_upload(
    engine: SyncEngine, drive: MagicMock, vault: Path
) -> None:
    _write(vault, "x.md", b"x")
    engine.handle_local_change("modified", "x.md")
    drive.upload.assert_called_once()


def test_handle_local_change_deleted_uses_state_drive_id(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    state.files["x.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")
    engine.handle_local_change("deleted", "x.md")
    drive.move_to_tombstones.assert_called_once()


def test_handle_local_change_deleted_unknown_file_noop(
    engine: SyncEngine, drive: MagicMock
) -> None:
    engine.handle_local_change("deleted", "unknown.md")
    drive.move_to_tombstones.assert_not_called()


def test_handle_local_change_unsupported_event(
    engine: SyncEngine, drive: MagicMock
) -> None:
    engine.handle_local_change("weird_event", "x.md")
    drive.upload.assert_not_called()
    drive.move_to_tombstones.assert_not_called()


# ── handle_remote_changes ───────────────────────────────────────────────


def test_remote_removed_known_file_triggers_delete_local(
    engine: SyncEngine, state: SyncState, vault: Path
) -> None:
    _write(vault, "x.md", b"x")
    state.files["x.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")

    engine.handle_remote_changes(
        [{"file_id": "did", "removed": True, "file": None}]
    )

    assert not (vault / "x.md").exists()
    # v2: deleted=True로 tombstone 기록
    assert "x.md" in state.files
    assert state.files["x.md"].deleted is True


def test_remote_removed_unknown_file_ignored(
    engine: SyncEngine, state: SyncState
) -> None:
    engine.handle_remote_changes(
        [{"file_id": "unknown_id", "removed": True, "file": None}]
    )
    assert len(state.files) == 0


def test_remote_modified_known_file_triggers_download(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    state.files["x.md"] = FileEntry(mtime=1.0, size=1, drive_id="did")

    def _download(file_id: str, local_path: Path) -> None:
        local_path.write_bytes(b"new")

    drive.download.side_effect = _download

    engine.handle_remote_changes(
        [
            {
                "file_id": "did",
                "removed": False,
                "file": {"name": "x.md", "modified_time": "t", "md5": "m"},
            }
        ]
    )
    assert (vault / "x.md").read_bytes() == b"new"


def test_remote_new_file_downloads_by_name(
    engine: SyncEngine, drive: MagicMock, vault: Path
) -> None:
    def _download(file_id: str, local_path: Path) -> None:
        local_path.write_bytes(b"new")

    drive.download.side_effect = _download

    engine.handle_remote_changes(
        [
            {
                "file_id": "newid",
                "removed": False,
                "file": {"name": "new.md", "modified_time": "t", "md5": "m"},
            }
        ]
    )
    assert (vault / "new.md").exists()


def test_unknown_action_type_is_logged(
    engine: SyncEngine, drive: MagicMock
) -> None:
    engine.execute({"type": "nonsense"})
    # 예외 없이 종료 + 잠금 해제
    assert engine.lock is False


# ── Sprint 3: 404 정리 ──────────────────────────────────────────────────


def test_delete_remote_404_removes_state_entry(
    engine: SyncEngine, state: SyncState, drive: MagicMock, vault: Path
) -> None:
    """delete_remote가 404를 받으면 sync_engine이 state에서 해당 drive_id를 제거한다."""
    state.files["note.md"] = FileEntry(mtime=1.0, size=1, drive_id="missing")
    drive.move_to_tombstones.side_effect = DriveFileNotFoundError("missing")

    engine.execute(
        {
            "type": ACTION_DELETE_REMOTE,
            "file_id": "missing",
            "path": "note.md",
        }
    )

    assert "note.md" not in state.files
    assert engine.lock is False


def test_download_404_removes_state_entry(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    """download 중 404 발생 시 state에서 drive_id 제거."""
    state.files["gone.md"] = FileEntry(mtime=1.0, size=1, drive_id="fid_gone")
    drive.download.side_effect = DriveFileNotFoundError("fid_gone")

    engine.execute(
        {"type": ACTION_DOWNLOAD, "file_id": "fid_gone", "path": "gone.md"}
    )

    assert "gone.md" not in state.files


def test_rename_404_removes_state_entry(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    """rename_remote에서 404 발생 시 대상 경로의 state 정리."""
    state.files["old.md"] = FileEntry(mtime=1.0, size=1, drive_id="fid_missing")
    drive.rename.side_effect = DriveFileNotFoundError("fid_missing")

    engine.execute(
        {
            "type": ACTION_RENAME_REMOTE,
            "old_path": "old.md",
            "new_path": "new.md",
        }
    )

    # 존재하지 않는 drive_id → state에서 제거
    assert "old.md" not in state.files


def test_404_cleanup_without_path_hint(
    engine: SyncEngine, state: SyncState, drive: MagicMock
) -> None:
    """path 힌트가 없어도 drive_id 전체 스캔으로 정리한다."""
    state.files["a.md"] = FileEntry(mtime=1.0, size=1, drive_id="orphan")
    state.files["b.md"] = FileEntry(mtime=2.0, size=2, drive_id="keep")
    drive.hard_delete.side_effect = DriveFileNotFoundError("orphan")

    engine.execute({"type": ACTION_DELETE_REMOTE, "file_id": "orphan"})

    assert "a.md" not in state.files
    assert "b.md" in state.files
