"""TrashManager 단위 테스트.

move → 파일+메타 존재, gc 30일 경과 삭제, should_ignore 확인,
restore 복원, list_entries 목록 반환을 검증한다.
"""
from __future__ import annotations

import json
import time

import pytest

from src.config import should_ignore
from src.trash import TrashManager


@pytest.fixture
def vault(tmp_path):
    """테스트 볼트 디렉토리."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return vault_dir


@pytest.fixture
def trash_mgr(vault):
    """TrashManager 인스턴스."""
    return TrashManager(vault)


class TestMove:
    """TrashManager.move() 테스트."""

    def test_move_creates_body_and_meta(self, vault, trash_mgr):
        """이동 후 trash에 본체 + 메타 JSON이 생성된다."""
        f = vault / "note.md"
        f.write_text("hello", encoding="utf-8")

        entry_id = trash_mgr.move(f, "note.md")

        body = trash_mgr.trash_dir / entry_id
        meta = trash_mgr.trash_dir / f"{entry_id}.json"
        assert body.exists()
        assert meta.exists()
        # 원본은 삭제됨
        assert not f.exists()

    def test_move_meta_has_correct_fields(self, vault, trash_mgr):
        """메타 JSON에 필수 필드가 포함된다."""
        f = vault / "sub" / "doc.md"
        f.parent.mkdir()
        f.write_text("content", encoding="utf-8")

        entry_id = trash_mgr.move(f, "sub/doc.md", md5="abc123")

        meta_path = trash_mgr.trash_dir / f"{entry_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["original_path"] == "sub/doc.md"
        assert meta["mtime"] > 0
        assert meta["size"] == len(b"content")
        assert meta["deleted_at"] > 0
        assert meta["md5"] == "abc123"

    def test_move_without_md5(self, vault, trash_mgr):
        """md5 없이 이동해도 정상."""
        f = vault / "note.md"
        f.write_text("x", encoding="utf-8")
        entry_id = trash_mgr.move(f, "note.md")

        meta_path = trash_mgr.trash_dir / f"{entry_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "md5" not in meta

    def test_move_nonexistent_raises(self, vault, trash_mgr):
        """존재하지 않는 파일 이동 시 FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            trash_mgr.move(vault / "nope.md", "nope.md")

    def test_move_preserves_content(self, vault, trash_mgr):
        """이동된 파일의 내용이 보존된다."""
        f = vault / "note.md"
        f.write_text("important content", encoding="utf-8")

        entry_id = trash_mgr.move(f, "note.md")

        body = trash_mgr.trash_dir / entry_id
        assert body.read_text(encoding="utf-8") == "important content"

    def test_move_korean_filename(self, vault, trash_mgr):
        """한국어 파일명도 정상 이동."""
        f = vault / "메모.md"
        f.write_text("중요", encoding="utf-8")

        entry_id = trash_mgr.move(f, "메모.md")

        meta_path = trash_mgr.trash_dir / f"{entry_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["original_path"] == "메모.md"


class TestGC:
    """TrashManager.gc() 테스트."""

    def test_gc_removes_expired_items(self, vault, trash_mgr):
        """30일 경과 항목만 삭제된다."""
        f = vault / "old.md"
        f.write_text("old", encoding="utf-8")
        entry_id = trash_mgr.move(f, "old.md")

        # 메타의 deleted_at을 31일 전으로 조작
        meta_path = trash_mgr.trash_dir / f"{entry_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["deleted_at"] = time.time() - (31 * 86400)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        removed = trash_mgr.gc()
        assert removed == 1
        assert not (trash_mgr.trash_dir / entry_id).exists()
        assert not meta_path.exists()

    def test_gc_keeps_recent_items(self, vault, trash_mgr):
        """30일 미경과 항목은 유지된다."""
        f = vault / "recent.md"
        f.write_text("recent", encoding="utf-8")
        entry_id = trash_mgr.move(f, "recent.md")

        removed = trash_mgr.gc()
        assert removed == 0
        assert (trash_mgr.trash_dir / entry_id).exists()

    def test_gc_mixed_items(self, vault, trash_mgr):
        """경과+미경과 혼합 시 경과 항목만 삭제."""
        now = time.time()

        # 오래된 항목
        f1 = vault / "old.md"
        f1.write_text("old", encoding="utf-8")
        old_id = trash_mgr.move(f1, "old.md")
        meta1 = trash_mgr.trash_dir / f"{old_id}.json"
        m = json.loads(meta1.read_text(encoding="utf-8"))
        m["deleted_at"] = now - (60 * 86400)
        meta1.write_text(json.dumps(m), encoding="utf-8")

        # 최근 항목
        f2 = vault / "new.md"
        f2.write_text("new", encoding="utf-8")
        new_id = trash_mgr.move(f2, "new.md")

        removed = trash_mgr.gc(now=now)
        assert removed == 1
        assert not (trash_mgr.trash_dir / old_id).exists()
        assert (trash_mgr.trash_dir / new_id).exists()

    def test_gc_empty_trash(self, trash_mgr):
        """빈 trash에서 gc 호출 시 0 반환."""
        assert trash_mgr.gc() == 0

    def test_gc_custom_retention(self, vault, trash_mgr):
        """커스텀 retention 기간 적용."""
        f = vault / "note.md"
        f.write_text("x", encoding="utf-8")
        entry_id = trash_mgr.move(f, "note.md")

        # 2일 전으로 설정
        meta_path = trash_mgr.trash_dir / f"{entry_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["deleted_at"] = time.time() - (2 * 86400)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        # 1일 retention → 삭제됨
        assert trash_mgr.gc(retention_days=1) == 1
        assert not (trash_mgr.trash_dir / entry_id).exists()


class TestListEntries:
    """TrashManager.list_entries() 테스트."""

    def test_list_empty(self, trash_mgr):
        assert trash_mgr.list_entries() == []

    def test_list_entries(self, vault, trash_mgr):
        for name in ("a.md", "b.md"):
            f = vault / name
            f.write_text(name, encoding="utf-8")
            trash_mgr.move(f, name)

        entries = trash_mgr.list_entries()
        assert len(entries) == 2
        paths = {e.original_path for e in entries}
        assert paths == {"a.md", "b.md"}


class TestRestore:
    """TrashManager.restore() 테스트."""

    def test_restore_recovers_file(self, vault, trash_mgr):
        f = vault / "note.md"
        f.write_text("important", encoding="utf-8")
        entry_id = trash_mgr.move(f, "note.md")

        target = vault / "restored.md"
        trash_mgr.restore(entry_id, target)

        assert target.read_text(encoding="utf-8") == "important"
        assert not (trash_mgr.trash_dir / entry_id).exists()
        assert not (trash_mgr.trash_dir / f"{entry_id}.json").exists()

    def test_restore_creates_parent_dirs(self, vault, trash_mgr):
        f = vault / "note.md"
        f.write_text("x", encoding="utf-8")
        entry_id = trash_mgr.move(f, "note.md")

        target = vault / "deep" / "path" / "note.md"
        trash_mgr.restore(entry_id, target)
        assert target.exists()

    def test_restore_nonexistent_raises(self, trash_mgr, vault):
        with pytest.raises(FileNotFoundError):
            trash_mgr.restore("nonexistent-uuid", vault / "x.md")


class TestShouldIgnoreTrash:
    """should_ignore가 .sync/trash/ 경로를 제외하는지 확인."""

    def test_trash_file_ignored(self):
        assert should_ignore(".sync/trash/abc-123") is True

    def test_trash_meta_ignored(self):
        assert should_ignore(".sync/trash/abc-123.json") is True

    def test_sync_dir_ignored(self):
        assert should_ignore(".sync/sync_state.json") is True
