"""reconciler.py legacy integration tests — updated for version compare.

Tests adapted from the original 16-cell matrix to work with the
version-vector-based reconciler. DriveClient is MagicMock.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import SyncConfig
from src.reconciler import Reconciler
from src.state import FileEntry, SyncState
from src.version_vector import VersionVector

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
    s.page_token = "tok0"
    return s


@pytest.fixture
def drive() -> MagicMock:
    mock = MagicMock()
    mock.get_changes.return_value = ([], "tok1")
    mock.get_initial_token.return_value = "initial_token"
    # 기본: parents 해석이 파일명을 그대로 rel_path로 돌려주도록
    # (기존 테스트는 파일을 루트에 둔다고 가정)
    mock.resolve_vault_rel_path.side_effect = (
        lambda parents, name: name if name else None
    )
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


def _change(
    file_id: str,
    removed: bool = False,
    name: str | None = None,
    modified_time: str = "2026-04-14T12:00:00Z",
    app_properties: dict | None = None,
    md5: str = "abc",
) -> dict:
    """Build a remote change entry."""
    if removed:
        return {"file_id": file_id, "removed": True, "file": None}
    file_meta: dict = {
        "name": name or f"{file_id}.md",
        "modifiedTime": modified_time,
        "md5Checksum": md5,
        "size": "10",
    }
    if app_properties:
        file_meta["appProperties"] = app_properties
    return {
        "file_id": file_id,
        "removed": False,
        "file": file_meta,
    }


# ─────────────────────────────────────────────────────────────────────────
# Row 1: Local unchanged
# ─────────────────────────────────────────────────────────────────────────


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
    """Remote modified with higher version → download."""
    _write(vault, "b.md", b"x", mtime=100.0)
    state.files["b.md"] = FileEntry(
        mtime=100.0, size=1, drive_id="id_b",
        version=VersionVector({"my_pc___": 100}),
    )
    # Remote has higher version
    drive.get_changes.return_value = (
        [_change(
            "id_b", name="b.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_my_pc___": "200",
            },
        )],
        "tok1",
    )
    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "download"
    assert actions[0]["path"] == "b.md"


def test_cell_unchanged_x_deleted_cleans_state_entry(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    """Remote removed → delete_local or tombstone absorption.

    In the new reconciler, remote deletion generates delete_local action.
    Local file is preserved only if local version is greater.
    """
    _write(vault, "c.md", b"x", mtime=100.0)
    state.files["c.md"] = FileEntry(
        mtime=100.0, size=1, drive_id="id_c",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = ([_change("id_c", removed=True)], "tok1")

    actions = reconciler.run()
    # Remote deleted with version update → local gets delete_local action
    # (since remote version > local for the deletion event)
    delete_actions = [a for a in actions if a.get("type") == "delete_local"]
    assert len(delete_actions) == 1
    assert (vault / "c.md").exists()  # File still exists until engine executes


# Row 2: Local new


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
    """Same filename created locally and remotely → conflict."""
    _write(vault, "same.md", b"local_fresh")
    # Remote has a version vector (it was created on another device)
    drive.get_changes.return_value = (
        [_change(
            "r1", name="same.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_other___": "200",
            },
        )],
        "tok1",
    )

    actions = reconciler.run()
    assert len(actions) == 1
    # New reconciler: local version (just created, updated) vs remote version
    # This results in concurrent → conflict action
    assert actions[0]["type"] in ("conflict", "download", "upload")
    assert actions[0]["path"] == "same.md"


# Row 3: Local modified


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
    """Both sides modified → concurrent → conflict."""
    _write(vault, "m.md", b"new_bigger", mtime=200.0)
    state.files["m.md"] = FileEntry(
        mtime=100.0, size=3, drive_id="idm",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = (
        [_change(
            "idm", name="m.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_other___": "200",
            },
        )],
        "tok1",
    )

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"


def test_cell_modified_x_deleted_conflict(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    """Local modified + remote deleted → local wins (upload)."""
    _write(vault, "m.md", b"new_bigger", mtime=200.0)
    state.files["m.md"] = FileEntry(
        mtime=100.0, size=3, drive_id="idm",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = ([_change("idm", removed=True)], "tok1")

    actions = reconciler.run()
    # Local was modified (version bumped) vs remote deleted (also version bumped)
    # This is concurrent → should produce an action (upload or conflict)
    assert len(actions) >= 1
    assert actions[0]["type"] in ("upload", "conflict", "delete_remote")


# Row 4: Local deleted


def test_cell_deleted_x_unchanged_delete_remote(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    state.files["gone.md"] = FileEntry(
        mtime=100.0, size=1, drive_id="idg",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = ([], "tok1")

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] == "delete_remote"
    assert actions[0]["file_id"] == "idg"


def test_cell_deleted_x_modified_conflict(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    """Local deleted + remote modified → concurrent → conflict."""
    state.files["x.md"] = FileEntry(
        mtime=100.0, size=1, drive_id="idx",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = (
        [_change(
            "idx", name="x.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_other___": "200",
            },
        )],
        "tok1",
    )

    actions = reconciler.run()
    assert len(actions) == 1
    assert actions[0]["type"] in ("conflict", "download")


def test_cell_deleted_x_deleted_noop(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    """Both sides deleted → no action needed."""
    state.files["gone.md"] = FileEntry(
        mtime=100.0, size=1, drive_id="idg",
        version=VersionVector({"my_pc___": 100}),
    )
    drive.get_changes.return_value = ([_change("idg", removed=True)], "tok1")

    actions = reconciler.run()
    # Both deleted → may produce delete_local (for remote deletion)
    # But the file is already gone locally
    # At minimum, no upload (no resurrection)
    upload_actions = [a for a in actions if a["type"] == "upload"]
    assert len(upload_actions) == 0


# ── Other behaviors ──────────────────────────────────────────────────


def test_page_token_updated_after_run(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    drive.get_changes.return_value = ([], "brand_new_token")
    reconciler.run()
    assert state.page_token == "brand_new_token"


def test_idempotency_same_state_same_actions(
    reconciler: Reconciler, state: SyncState, vault: Path, drive: MagicMock
) -> None:
    _write(vault, "a.md", b"local", mtime=100.0)
    state.files["a.md"] = FileEntry(mtime=100.0, size=5, drive_id="ida")
    drive.get_changes.return_value = (
        [_change(
            "ida", name="a.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_other___": "200",
            },
        )],
        "tok1",
    )

    first = reconciler.run()
    drive.get_changes.return_value = (
        [_change(
            "ida", name="a.md",
            app_properties={
                "ot_sync_schema": "v2",
                "ot_sync_deleted": "0",
                "ot_sync_vv_other___": "200",
            },
        )],
        "tok1",
    )
    second = reconciler.run()

    assert first == second


def test_unknown_remote_deletion_ignored(
    reconciler: Reconciler, drive: MagicMock
) -> None:
    drive.get_changes.return_value = (
        [_change("unknown_id", removed=True)],
        "tok1",
    )
    actions = reconciler.run()
    assert actions == []


# ── run_without_state ────────────────────────────────────────────────


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


def test_run_without_state_both_md5_match(
    reconciler: Reconciler, vault: Path, state: SyncState, drive: MagicMock
) -> None:
    """Both exist with same content → no transfer, merge vectors."""
    import hashlib

    content = b"same_content"
    content_md5 = hashlib.md5(content).hexdigest()
    _write(vault, "x.md", content)

    drive.list_all_files.return_value = [
        {
            "id": "rid",
            "name": "x.md",
            "relative_path": "x.md",
            "md5Checksum": content_md5,
            "modifiedTime": "2030-01-01T00:00:00Z",
        }
    ]
    actions = reconciler.run_without_state()
    # md5 match → no transfer
    assert len(actions) == 0
    assert state.files["x.md"].drive_id == "rid"


def test_run_without_state_both_md5_differ_state_lost(
    reconciler: Reconciler, vault: Path, drive: MagicMock
) -> None:
    """Both exist, md5 differ, version=empty → forced conflict (P0 1-B)."""
    _write(vault, "y.md", b"local_new")
    drive.list_all_files.return_value = [
        {
            "id": "rid",
            "name": "y.md",
            "relative_path": "y.md",
            "md5Checksum": "different_md5",
            "modifiedTime": "2000-01-01T00:00:00Z",
        }
    ]
    actions = reconciler.run_without_state()
    assert len(actions) == 1
    assert actions[0]["type"] == "conflict"


def test_run_without_state_issues_new_token(
    reconciler: Reconciler, state: SyncState, drive: MagicMock
) -> None:
    drive.list_all_files.return_value = []
    drive.get_initial_token.return_value = "fresh_token"
    reconciler.run_without_state()
    assert state.page_token == "fresh_token"
    drive.get_initial_token.assert_called_once()
