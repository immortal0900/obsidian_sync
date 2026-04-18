"""tests for reconciler v2 — version compare based decide().

Covers:
- decide(): Equal/Greater/Lesser/ConcurrentGreater/ConcurrentLesser
- decide_download_or_delete / decide_upload_or_delete (tombstone)
- UpdateVectorOnly (md5+size match)
- resolve_conflict HLC tiebreaker + device prefix fallback
- run_without_state 5 branches
- Symptom 3 & 4 E2E (delete + restart → no resurrection)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.reconciler import (
    AbsorbRemoteTombstone,
    DeleteLocal,
    DeleteRemote,
    Download,
    NoOp,
    Reconciler,
    UpdateVectorOnly,
    Upload,
    decide,
    decide_download_or_delete,
    decide_upload_or_delete,
    resolve_conflict,
)
from src.state import FileEntry, SyncState
from src.version_vector import VersionVector

# ── helpers ───────────────────────────────────────────────────────────

DEV_A = "deviceAA"  # prefix: "deviceAA"
DEV_B = "deviceBB"  # prefix: "deviceBB"


def _fe(
    *,
    version: VersionVector | None = None,
    deleted: bool = False,
    md5: str | None = None,
    size: int = 100,
    drive_id: str | None = None,
    mtime: float = 1000.0,
) -> FileEntry:
    return FileEntry(
        mtime=mtime,
        size=size,
        drive_id=drive_id,
        version=version or VersionVector.empty(),
        deleted=deleted,
        md5=md5,
    )


# ── decide() unit tests ──────────────────────────────────────────────


class TestDecideEqual:
    def test_both_none_returns_noop(self):
        assert isinstance(decide(None, None), NoOp)

    def test_equal_vectors_returns_noop(self):
        v = VersionVector({"deviceAA": 100})
        local = _fe(version=v)
        remote = _fe(version=v)
        assert isinstance(decide(local, remote), NoOp)


class TestDecideGreater:
    def test_local_greater_returns_upload(self):
        local = _fe(version=VersionVector({"deviceAA": 200}))
        remote = _fe(version=VersionVector({"deviceAA": 100}))
        action = decide(local, remote)
        assert isinstance(action, Upload)
        assert action.conflict_copy_of is None

    def test_local_deleted_greater_returns_delete_remote(self):
        local = _fe(version=VersionVector({"deviceAA": 200}), deleted=True)
        remote = _fe(version=VersionVector({"deviceAA": 100}), drive_id="r1")
        action = decide(local, remote)
        assert isinstance(action, DeleteRemote)


class TestDecideLesser:
    def test_remote_greater_returns_download(self):
        local = _fe(version=VersionVector({"deviceAA": 100}))
        remote = _fe(version=VersionVector({"deviceAA": 200}), drive_id="r1")
        action = decide(local, remote)
        assert isinstance(action, Download)
        assert action.file_id == "r1"

    def test_remote_deleted_lesser_returns_delete_local(self):
        local = _fe(version=VersionVector({"deviceAA": 100}))
        remote = _fe(version=VersionVector({"deviceAA": 200}), deleted=True)
        action = decide(local, remote)
        assert isinstance(action, DeleteLocal)


class TestDecideConcurrent:
    def test_concurrent_triggers_resolve_conflict(self):
        # A updated by devA, B updated by devB → concurrent
        local = _fe(version=VersionVector({"deviceAA": 300, "deviceBB": 100}))
        remote = _fe(version=VersionVector({"deviceAA": 100, "deviceBB": 300}), drive_id="r1")
        action = decide(local, remote)
        # This should be Upload or Download with conflict_copy_of set
        assert isinstance(action, (Upload, Download))
        assert action.conflict_copy_of is not None


class TestDecideUpdateVectorOnly:
    def test_md5_match_returns_update_vector_only(self):
        local = _fe(
            version=VersionVector({"deviceAA": 100}),
            md5="abc123",
            size=50,
        )
        remote = _fe(
            version=VersionVector({"deviceBB": 200}),
            md5="abc123",
            size=50,
        )
        action = decide(local, remote)
        assert isinstance(action, UpdateVectorOnly)
        assert action.merged is not None
        # Merged should have both devices
        assert action.merged.counters.get("deviceAA") == 100
        assert action.merged.counters.get("deviceBB") == 200

    def test_md5_none_does_not_match(self):
        local = _fe(version=VersionVector({"deviceAA": 200}), md5=None, size=50)
        remote = _fe(version=VersionVector({"deviceAA": 100}), md5=None, size=50)
        action = decide(local, remote)
        # md5 is None → cannot determine content match → fall through to compare
        assert not isinstance(action, UpdateVectorOnly)


# ── decide_download_or_delete / decide_upload_or_delete ──────────────


class TestDecideDownloadOrDelete:
    def test_remote_deleted_absorbs_tombstone(self):
        remote = _fe(deleted=True, version=VersionVector({"deviceAA": 100}))
        action = decide_download_or_delete(remote)
        assert isinstance(action, AbsorbRemoteTombstone)

    def test_remote_alive_downloads(self):
        remote = _fe(deleted=False, drive_id="r1")
        action = decide_download_or_delete(remote)
        assert isinstance(action, Download)
        assert action.file_id == "r1"


class TestDecideUploadOrDelete:
    def test_local_deleted_returns_noop(self):
        local = _fe(deleted=True)
        action = decide_upload_or_delete(local)
        assert isinstance(action, NoOp)

    def test_local_alive_returns_upload(self):
        local = _fe(deleted=False)
        action = decide_upload_or_delete(local)
        assert isinstance(action, Upload)


# ── resolve_conflict HLC tiebreaker ──────────────────────────────────


class TestResolveConflict:
    def test_higher_hlc_wins_local(self):
        """Local has higher HLC → local wins → Upload."""
        local = _fe(version=VersionVector({"deviceAA": 500, "deviceBB": 100}))
        remote = _fe(version=VersionVector({"deviceAA": 100, "deviceBB": 300}), drive_id="r1")
        action = resolve_conflict(local, remote)
        assert isinstance(action, Upload)
        assert action.conflict_copy_of is remote

    def test_higher_hlc_wins_remote(self):
        """Remote has higher HLC → remote wins → Download."""
        local = _fe(version=VersionVector({"deviceAA": 200, "deviceBB": 100}))
        remote = _fe(version=VersionVector({"deviceAA": 100, "deviceBB": 500}), drive_id="r1")
        action = resolve_conflict(local, remote)
        assert isinstance(action, Download)
        assert action.conflict_copy_of is local

    def test_hlc_tie_device_prefix_fallback(self):
        """HLC tie → device prefix lexicographic larger loses."""
        # Both max HLC = 300
        local = _fe(version=VersionVector({"zzzzdevA": 300}))
        remote = _fe(version=VersionVector({"aaaadevB": 300}), drive_id="r1")
        action = resolve_conflict(local, remote)
        # local prefix "zzzzdevA" > remote prefix "aaaadevB" → local loses → Download
        assert isinstance(action, Download)
        assert action.conflict_copy_of is local

    def test_hlc_tie_other_direction(self):
        """HLC tie → smaller prefix wins."""
        local = _fe(version=VersionVector({"aaaa0001": 300}))
        remote = _fe(version=VersionVector({"zzzz0001": 300}), drive_id="r1")
        action = resolve_conflict(local, remote)
        # local prefix "aaaa0001" < remote prefix "zzzz0001" → local wins → Upload
        assert isinstance(action, Upload)
        assert action.conflict_copy_of is remote

    def test_mtime_manipulation_does_not_affect_result(self):
        """Wall-clock mtime is irrelevant; only HLC counters matter."""
        # Local has mtime far in the future, but lower HLC
        local = _fe(
            version=VersionVector({"deviceAA": 100, "deviceBB": 100}),
            mtime=9999999999.0,
        )
        remote = _fe(
            version=VersionVector({"deviceAA": 100, "deviceBB": 500}),
            mtime=1000.0,
            drive_id="r1",
        )
        action = resolve_conflict(local, remote)
        # Remote has higher HLC (500 > 100) → remote wins
        assert isinstance(action, Download)
        assert action.conflict_copy_of is local

    def test_empty_vectors_fallback(self):
        """Both empty → HLC tie (0==0), device prefix fallback."""
        local = _fe(version=VersionVector.empty())
        remote = _fe(version=VersionVector.empty(), drive_id="r1")
        action = resolve_conflict(local, remote)
        # Both empty → HLC=0, prefix="" → Upload (local prefix "" <= remote prefix "")
        assert isinstance(action, Upload)


# ── run_without_state scenarios ──────────────────────────────────────


@pytest.fixture
def mock_config(tmp_path: Path):
    """Minimal SyncConfig-like object."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config = MagicMock()
    config.vault_path = vault
    config.state_dir = vault / ".sync"
    config.state_file = vault / ".sync" / "sync_state.json"
    config.device_id = "test_dev"
    config.trash_retention_days = 30
    return config


@pytest.fixture
def state(mock_config) -> SyncState:
    return SyncState(mock_config)


@pytest.fixture
def mock_drive():
    return MagicMock()


def _write_file(vault: Path, rel: str, content: bytes = b"hello") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class TestRunWithoutState:
    def test_branch1_md5_match_no_transfer(self, state, mock_drive):
        """Both exist + md5 match → no transfer, vector merge."""
        vault = state.vault_path
        _write_file(vault, "note.md", b"same content")

        # Remote has same md5
        import hashlib
        content_md5 = hashlib.md5(b"same content").hexdigest()

        mock_drive.list_all_files.return_value = [
            {
                "id": "r1",
                "name": "note.md",
                "relative_path": "note.md",
                "md5Checksum": content_md5,
                "modifiedTime": "2026-01-01T00:00:00Z",
                "size": "12",
                "appProperties": {
                    "ot_sync_schema": "v2",
                    "ot_sync_deleted": "0",
                    "ot_sync_vv_deviceBB": "200",
                },
            }
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        assert len(actions) == 0
        # State should have merged version
        entry = state.files.get("note.md")
        assert entry is not None
        assert entry.drive_id == "r1"

    def test_branch2_local_only_upload(self, state, mock_drive):
        """Local only → upload."""
        vault = state.vault_path
        _write_file(vault, "local_only.md", b"local")

        mock_drive.list_all_files.return_value = []
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        assert len(actions) == 1
        assert actions[0]["type"] == "upload"
        assert actions[0]["path"] == "local_only.md"

    def test_branch3_remote_only_download(self, state, mock_drive):
        """Remote only → download."""
        mock_drive.list_all_files.return_value = [
            {
                "id": "r1",
                "name": "remote_only.md",
                "relative_path": "remote_only.md",
                "md5Checksum": "abc",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "size": "10",
            }
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        assert len(actions) == 1
        assert actions[0]["type"] == "download"
        assert actions[0]["file_id"] == "r1"

    def test_branch4_state_lost_conflict(self, state, mock_drive):
        """State lost + md5 differ + empty version → forced Conflict."""
        vault = state.vault_path
        _write_file(vault, "edited.md", b"local edited version")

        mock_drive.list_all_files.return_value = [
            {
                "id": "r1",
                "name": "edited.md",
                "relative_path": "edited.md",
                "md5Checksum": "different_md5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "size": "20",
            }
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        assert len(actions) == 1
        assert actions[0]["type"] == "conflict"
        assert actions[0]["reason"] == "init_state_lost_conflict"

    def test_branch5_tombstone_only_absorb(self, state, mock_drive):
        """Tombstone only → absorb deleted=True in state."""
        mock_drive.list_all_files.return_value = [
            {
                "id": "t1",
                "name": "deleted_file.md",
                "relative_path": "deleted_file.md",
                "appProperties": {
                    "ot_sync_schema": "v2",
                    "ot_sync_deleted": "1",
                    "ot_sync_vv_deviceBB": "300",
                },
            }
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        # No file action needed
        assert len(actions) == 0
        # But state should have the tombstone
        entry = state.files.get("deleted_file.md")
        assert entry is not None
        assert entry.deleted is True

    def test_ignore_patterns_applied(self, state, mock_drive):
        """IGNORE_PATTERNS should filter remote files."""
        vault = state.vault_path
        _write_file(vault, "normal.md", b"ok")

        mock_drive.list_all_files.return_value = [
            {
                "id": "r1",
                "name": ".obsidian/workspace.json",
                "relative_path": ".obsidian/workspace.json",
                "md5Checksum": "x",
            },
            {
                "id": "r2",
                "name": "normal.md",
                "relative_path": "normal.md",
                "md5Checksum": "different",
            },
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        # .obsidian/ should be ignored
        paths = [a["path"] for a in actions]
        assert ".obsidian/workspace.json" not in paths


# ── Symptom 3 & 4 E2E tests ─────────────────────────────────────────


class TestSymptomPrevention:
    """Verify that deleted files don't resurrect after restart."""

    def test_symptom3_local_delete_no_resurrection(self, state, mock_drive):
        """Symptom 3: local delete → restart → file should NOT come back.

        Scenario: File was deleted locally, tombstone is in Drive.
        After restart (run_without_state), file should stay deleted.
        """
        # No local file exists
        # Remote has tombstone
        mock_drive.list_all_files.return_value = [
            {
                "id": "t1",
                "name": "deleted.md",
                "relative_path": "deleted.md",
                "appProperties": {
                    "ot_sync_schema": "v2",
                    "ot_sync_deleted": "1",
                    "ot_sync_vv_test_dev": "500",
                },
            }
        ]
        mock_drive.get_initial_token.return_value = "token1"

        r = Reconciler(state, mock_drive)
        actions = r.run_without_state()

        # Should NOT produce a download action (resurrection)
        download_actions = [a for a in actions if a.get("type") == "download"]
        assert len(download_actions) == 0

        # Tombstone should be in state
        entry = state.files.get("deleted.md")
        assert entry is not None
        assert entry.deleted is True

    def test_symptom4_remote_delete_no_resurrection(self, state, mock_drive):
        """Symptom 4: remote delete → restart → file should NOT be re-uploaded.

        Scenario: File was deleted from Drive. Local has state with deleted=True.
        After run() with no remote changes, file should stay deleted.
        """
        # Pre-populate state with a tombstone entry
        state.files["deleted.md"] = FileEntry(
            mtime=0.0,
            size=0,
            drive_id="r1",
            version=VersionVector({"test_dev": 500}),
            deleted=True,
        )
        state.page_token = "token_old"

        # No remote changes
        mock_drive.get_changes.return_value = ([], "token_new")

        r = Reconciler(state, mock_drive)
        actions = r.run()

        # No upload action (resurrection) should be generated
        upload_actions = [a for a in actions if a.get("type") == "upload"]
        assert len(upload_actions) == 0

    def test_symptom3_incremental_local_delete_tombstone(self, state, mock_drive):
        """After local delete → remote reports deletion → no resurrection."""
        # File existed before, now deleted locally
        state.files["note.md"] = FileEntry(
            mtime=1000.0,
            size=10,
            drive_id="r1",
            version=VersionVector({"test_dev": 100}),
        )
        state.page_token = "token_old"

        # Remote says this file was removed
        mock_drive.get_changes.return_value = (
            [{"file_id": "r1", "removed": True}],
            "token_new",
        )

        r = Reconciler(state, mock_drive)
        actions = r.run()

        # Should get delete_local action (file already gone locally, but that's OK)
        # OR no action if local file is also gone
        # The key: no upload action (no resurrection)
        upload_actions = [a for a in actions if a.get("type") == "upload"]
        assert len(upload_actions) == 0


# ── Drive metadata-only change skip ─────────────────────────────────


class TestMetadataOnlySkip:
    def test_md5_same_modifiedtime_different_no_download(self, state, mock_drive):
        """Drive metadata change (md5 same, modifiedTime different) → skip."""
        vault = state.vault_path
        _write_file(vault, "note.md", b"same")

        import hashlib
        same_md5 = hashlib.md5(b"same").hexdigest()

        state.files["note.md"] = FileEntry(
            mtime=1000.0,
            size=4,
            drive_id="r1",
            version=VersionVector({"test_dev": 100}),
            md5=same_md5,
        )
        state.page_token = "token_old"

        # Remote reports change but md5 is the same
        mock_drive.get_changes.return_value = (
            [
                {
                    "file_id": "r1",
                    "removed": False,
                    "file": {
                        "name": "note.md",
                        "modifiedTime": "2026-06-01T00:00:00Z",
                        "md5Checksum": same_md5,
                        "size": "4",
                        "appProperties": {
                            "ot_sync_schema": "v2",
                            "ot_sync_deleted": "0",
                            "ot_sync_vv_test_dev": "100",
                        },
                    },
                }
            ],
            "token_new",
        )

        r = Reconciler(state, mock_drive)
        actions = r.run()

        # md5 match → UpdateVectorOnly → no actual transfer action
        download_actions = [a for a in actions if a.get("type") == "download"]
        assert len(download_actions) == 0
