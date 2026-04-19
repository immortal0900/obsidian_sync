"""Tests for src/hash.py — chunked MD5 computation."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from src.hash import DEFAULT_MAX_FILE_SIZE_MB, compute_md5


class TestComputeMd5:
    """Basic md5 computation."""

    def test_known_content(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("hello world", encoding="utf-8")
        expected = hashlib.md5(b"hello world").hexdigest()
        assert compute_md5(f) == expected

    def test_binary_content(self, tmp_path: Path) -> None:
        f = tmp_path / "bin.dat"
        data = bytes(range(256)) * 100
        f.write_bytes(data)
        expected = hashlib.md5(data).hexdigest()
        assert compute_md5(f) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.md5(b"").hexdigest()
        assert compute_md5(f) == expected

    def test_large_file_chunks(self, tmp_path: Path) -> None:
        """File larger than chunk size (8KB) still hashes correctly."""
        f = tmp_path / "large.bin"
        data = b"x" * 32768  # 32KB
        f.write_bytes(data)
        expected = hashlib.md5(data).hexdigest()
        assert compute_md5(f) == expected


class TestMaxBytes:
    """Size limit enforcement."""

    def test_exceeds_max_bytes(self, tmp_path: Path) -> None:
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * 200)
        assert compute_md5(f, max_bytes=100) is None

    def test_exactly_at_max_bytes(self, tmp_path: Path) -> None:
        f = tmp_path / "exact.bin"
        f.write_bytes(b"x" * 100)
        assert compute_md5(f, max_bytes=100) is not None

    def test_default_max_bytes(self) -> None:
        assert DEFAULT_MAX_FILE_SIZE_MB == 100


class TestErrorHandling:
    """Graceful None return on errors."""

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert compute_md5(tmp_path / "nope.txt") is None

    def test_directory_path(self, tmp_path: Path) -> None:
        d = tmp_path / "subdir"
        d.mkdir()
        assert compute_md5(d) is None

    @pytest.mark.skipif(os.name == "nt", reason="chmod not reliable on Windows")
    def test_no_read_permission(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("secret")
        f.chmod(0o000)
        try:
            assert compute_md5(f) is None
        finally:
            f.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_string_path_accepted(self, tmp_path: Path) -> None:
        f = tmp_path / "str.txt"
        f.write_text("abc", encoding="utf-8")
        expected = hashlib.md5(b"abc").hexdigest()
        assert compute_md5(str(f)) == expected
