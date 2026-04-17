"""reconciler.py 테스트.

16셀 대조 규칙표의 모든 유효 조합 + run_without_state 시나리오를 검증한다.
DriveClient는 MagicMock으로 대체.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import SyncConfig
from src.reconciler import Reconciler
from src.state import FileEntry, SyncState

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
    # 기본 page_token 설정 — run()이 get_changes를 호출하도록
    s.page_token = "tok0"
    return s


@pytest.fixture
def drive() -> MagicMock:
    mock = MagicMock()
    mock.get_changes.return_value = ([], "tok1")
    mock.get_initial_token.return_value = "initial_token"
    return mock


@pytest.fixture
def reconciler(state: SyncState, drive: MagicMock) -> Reconciler:
    return Reconciler(state, drive)


def _write(vault: Path, rel: str, content: bytes = b"x", mtime: float | None = None) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    if mtime is not None:
        import os

        os.utime(p, (mtime, mtime))
    return p


def _change(file_id: str, removed: bool = False, name: str | None = None,
            modified_time: str = "2026-04-14T12:00:00Z") -> dict:
    """원격 변경 목록의 표준 스키마를 만든다."""
    if removed:
        return {"file_id": file_id, "removed": True, "file": None}
    return {
        "file_id": file_id,
        "removed": False,
        "file": {
            "name": name or f"{file_id}.md",
            "modified_time": modified_time,
            "md5": "abc",
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# 대조 규칙 16셀 (spec §5-6)
#
#                 | r_unch  | r_new   | r_mod   | r_del
# ----------------|---------|---------|---------|-------
# local_unch      | no-op   | down    | down    | no-op
# local_new       | upload  | conflict| n/a     | n/a
# local_mod       | upload  | n/a     | conflict| conflict
# local_del       | del_rem | n/a     | conflict| no-op
# ─────────────────────────────────────────────────────────────────────────


# 1행: 로컬 unchanged
def test_cell_unchanged_x_unchanged_noop(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "a.md", b"x", mtime=100.0)
    state.files["a.md"] = FileEntry(mtime=100.0, size=1, drive_id="id_a")
    drive.get_changes.return_value = ([], "tok1")

    actions = reconciler.run()
    assert actions == []


def test_cell_unchanged_x_new_download(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    drive.get_changes.return_value = (
        [_change("newid", name="remote_new.md")],
        "tok1",
    )
    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "download"
    assert actions[0]["path"] == "remote_new.md"
    assert actions[0]["file_id"] == "newid"


def test_cell_unchanged_x_modified_download(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "b.md", b"x", mtime=100.0)
    state.files["b.md"] = FileEntry(mtime=100.0, size=1, drive_id="id_b")
    drive.get_changes.return_value = ([_change("id_b", name="b.md")], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "download"
    assert actions[0]["path"] == "b.md"


def test_cell_unchanged_x_deleted_noop(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "c.md", b"x", mtime=100.0)
    state.files["c.md"] = FileEntry(mtime=100.0, size=1, drive_id="id_c")
    drive.get_changes.return_value = ([_change("id_c", removed=True)], "tok1")

    actions = reconciler.run()
    # spec: "이미 없음" → no-op
    assert actions == []


# 2행: 로컬 new
def test_cell_new_x_unchanged_upload(
    reconciler: Reconciler, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "fresh.md", b"new_local")
    drive.get_changes.return_value = ([], "tok1")
    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "upload"
    assert actions[0]["path"] == "fresh.md"


def test_cell_new_x_new_conflict(
    reconciler: Reconciler, vault: Path, drive: MagicMock
) -> None:
    """같은 이름의 파일이 로컬과 원격에서 동시에 생김."""
    _write(vault, "same.md", b"local_fresh")
    drive.get_changes.return_value = ([_change("r1", name="same.md")], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"
    assert actions[0]["path"] == "same.md"
    assert actions[0]["remote"]["file_id"] == "r1"
    assert actions[0]["local"]["size"] == len(b"local_fresh")


# 3행: 로컬 modified
def test_cell_modified_x_unchanged_upload(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "m.md", b"new_bigger", mtime=200.0)
    state.files["m.md"] = FileEntry(mtime=100.0, size=3, drive_id="idm")
    drive.get_changes.return_value = ([], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "upload"


def test_cell_modified_x_modified_conflict(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "m.md", b"new_bigger", mtime=200.0)
    state.files["m.md"] = FileEntry(mtime=100.0, size=3, drive_id="idm")
    drive.get_changes.return_value = ([_change("idm", name="m.md")], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"


def test_cell_modified_x_deleted_conflict(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "m.md", b"new_bigger", mtime=200.0)
    state.files["m.md"] = FileEntry(mtime=100.0, size=3, drive_id="idm")
    drive.get_changes.return_value = ([_change("idm", removed=True)], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"


# 4행: 로컬 deleted
def test_cell_deleted_x_unchanged_delete_remote(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    state.files["gone.md"] = FileEntry(mtime=100.0, size=1, drive_id="idg")
    # 로컬에서 파일 삭제됨 (_write 하지 않음)
    drive.get_changes.return_value = ([], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "delete_remote"
    assert actions[0]["file_id"] == "idg"


def test_cell_deleted_x_modified_conflict(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    state.files["x.md"] = FileEntry(mtime=100.0, size=1, drive_id="idx")
    drive.get_changes.return_value = ([_change("idx", name="x.md")], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"


def test_cell_deleted_x_deleted_noop(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    state.files["gone.md"] = FileEntry(mtime=100.0, size=1, drive_id="idg")
    drive.get_changes.return_value = ([_change("idg", removed=True)], "tok1")

    actions = reconciler.run()
    assert actions == []


# ── 기타 동작 ────────────────────────────────────────────────────────────


def test_page_token_updated_after_run(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    drive.get_changes.return_value = ([], "brand_new_token")
    reconciler.run()
    assert state.page_token == "brand_new_token"


def test_idempotency_same_state_same_actions(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    """같은 상태에서 두 번 run해도 동일 action 리스트가 나온다."""
    _write(vault, "a.md", b"local", mtime=100.0)
    state.files["a.md"] = FileEntry(mtime=100.0, size=5, drive_id="ida")
    drive.get_changes.return_value = ([_change("ida", name="a.md")], "tok1")

    first = reconciler.run()
    # drive.get_changes가 같은 값을 계속 반환하도록
    drive.get_changes.return_value = ([_change("ida", name="a.md")], "tok1")
    second = reconciler.run()

    assert first == second


def test_unknown_remote_deletion_ignored(
    reconciler: Reconciler, drive: MagicMock
) -> None:
    """알 수 없는 drive_id의 removed=True 변경은 무시된다."""
    drive.get_changes.return_value = (
        [_change("unknown_id", removed=True)],
        "tok1",
    )
    actions = reconciler.run()
    assert actions == []


# ── run_without_state ────────────────────────────────────────────────────


def test_run_without_state_local_only(
    reconciler: Reconciler, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "only_local.md", b"local")
    drive.list_all_files.return_value = []

    actions = reconciler.run_without_state()
    assert len(actions) == 1
    assert actions[0]["type"] == "upload"
    assert actions[0]["path"] == "only_local.md"


def test_run_without_state_remote_only(
    reconciler: Reconciler, drive: MagicMock
) -> None:
    drive.list_all_files.return_value = [
        {
            "id": "r1",
            "name": "remote_only.md",
            "relative_path": "remote_only.md",
            "modifiedTime": "2026-04-14T12:00:00Z",
        }
    ]
    actions = reconciler.run_without_state()
    assert len(actions) == 1
    assert actions[0]["type"] == "download"
    assert actions[0]["file_id"] == "r1"


def test_run_without_state_both_remote_newer(
    reconciler: Reconciler, vault: Path, state: SyncState, drive: MagicMock
) -> None:
    _write(vault, "x.md", b"local_old", mtime=100.0)  # UNIX epoch 100
    drive.list_all_files.return_value = [
        {
            "id": "rid",
            "name": "x.md",
            "relative_path": "x.md",
            "modifiedTime": "2030-01-01T00:00:00Z",  # 미래 → 원격이 더 최신
        }
    ]
    actions = reconciler.run_without_state()
    downloads = [a for a in actions if a["type"] == "download"]
    assert len(downloads) == 1
    # state에 drive_id가 기록됨
    assert state.files["x.md"].drive_id == "rid"


def test_run_without_state_both_local_newer(
    reconciler: Reconciler, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "y.md", b"local_new", mtime=9999999999.0)  # 매우 먼 미래
    drive.list_all_files.return_value = [
        {
            "id": "rid",
            "name": "y.md",
            "relative_path": "y.md",
            "modifiedTime": "2000-01-01T00:00:00Z",  # 과거
        }
    ]
    actions = reconciler.run_without_state()
    uploads = [a for a in actions if a["type"] == "upload"]
    assert len(uploads) == 1
    assert uploads[0]["path"] == "y.md"


def test_run_without_state_issues_new_token(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    drive.list_all_files.return_value = []
    drive.get_initial_token.return_value = "fresh_token"
    reconciler.run_without_state()
    assert state.page_token == "fresh_token"
    drive.get_initial_token.assert_called_once()
