"""conflict.py tests — Syncthing-style naming.

Verifies: naming format, uniqueness, edge cases, HLC tiebreaker (in reconciler).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.conflict import AUTO_RESOLVED, CONFLICT_CREATED, ConflictResolver

# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def resolver(vault: Path) -> ConflictResolver:
    return ConflictResolver(device_id="my_pc_device", vault_path=vault)


def _write(vault: Path, rel: str, content: bytes | str = b"hello") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)
    return p


# ── Syncthing naming ─────────────────────────────────────────────────


def test_syncthing_naming_format(resolver: ConflictResolver, vault: Path) -> None:
    """Format: {stem}.sync-conflict-{YYYYMMDD-HHMMSS}-{device_prefix}.{ext}"""
    _write(vault, "daily/note.md", b"local content")

    fixed_time = 1713100000.0
    with patch("src.conflict.time.time", return_value=fixed_time):
        result = resolver.resolve("daily/note.md", {"mtime": 1.0}, {"file_id": "r1"})

    assert result == CONFLICT_CREATED

    siblings = list((vault / "daily").iterdir())
    names = sorted(p.name for p in siblings)
    assert len(names) == 2
    assert "note.md" in names
    conflict_name = next(n for n in names if n != "note.md")
    # Must be Syncthing style: .sync-conflict-{ts}-{prefix}.md
    assert ".sync-conflict-" in conflict_name
    assert conflict_name.startswith("note.sync-conflict-")
    # device_prefix = "my_pc_de" (first 8 chars of "my_pc_device")
    assert "my_pc_de" in conflict_name
    assert conflict_name.endswith(".md")

    # Content preserved
    conflict_path = vault / "daily" / conflict_name
    assert conflict_path.read_bytes() == b"local content"


def test_device_prefix_8chars_in_name(vault: Path) -> None:
    """device_prefix (first 8 chars) is used, not the full device_id."""
    _write(vault, "note.md", b"x")
    r = ConflictResolver(device_id="galaxy_s25_ultra", vault_path=vault)
    r.resolve("note.md", {}, {})

    conflicts = [p.name for p in vault.iterdir() if p.name != "note.md"]
    assert len(conflicts) == 1
    # "galaxy_s" is the first 8 chars of "galaxy_s25_ultra"
    assert "galaxy_s" in conflicts[0]
    # Full id should NOT appear
    assert "galaxy_s25_ultra" not in conflicts[0]


def test_root_level_file(resolver: ConflictResolver, vault: Path) -> None:
    _write(vault, "root.md", b"x")
    result = resolver.resolve("root.md", {}, {})
    assert result == CONFLICT_CREATED

    conflict_files = [p for p in vault.iterdir() if p.name != "root.md" and p.is_file()]
    assert len(conflict_files) == 1
    assert ".sync-conflict-" in conflict_files[0].name


def test_file_without_extension(resolver: ConflictResolver, vault: Path) -> None:
    _write(vault, "notes/README", b"readme")
    result = resolver.resolve("notes/README", {}, {})
    assert result == CONFLICT_CREATED

    names = sorted(p.name for p in (vault / "notes").iterdir())
    assert "README" in names
    conflict_name = next(n for n in names if n != "README")
    assert conflict_name.startswith("README.sync-conflict-")
    # No trailing dot/extension
    assert conflict_name.endswith("my_pc_de")


# ── Uniqueness ────────────────────────────────────────────────────────


def test_same_second_creates_unique_names(
    resolver: ConflictResolver, vault: Path
) -> None:
    _write(vault, "note.md", b"a")

    fixed_time = 1713100000.0
    with patch("src.conflict.time.time", return_value=fixed_time):
        first = resolver.resolve("note.md", {}, {})
        second = resolver.resolve("note.md", {}, {})

    assert first == CONFLICT_CREATED
    assert second == CONFLICT_CREATED

    files = [p.name for p in vault.iterdir() if p.is_file()]
    assert len(files) == 3
    conflict_names = sorted(n for n in files if n != "note.md")
    assert len(conflict_names) == 2
    assert conflict_names[0] != conflict_names[1]


def test_one_second_apart_creates_distinct_names(
    resolver: ConflictResolver, vault: Path
) -> None:
    _write(vault, "note.md", b"a")

    with patch("src.conflict.time.time", return_value=1713100000.0):
        resolver.resolve("note.md", {}, {})
    with patch("src.conflict.time.time", return_value=1713100001.0):
        resolver.resolve("note.md", {}, {})

    files = sorted(p.name for p in vault.iterdir() if p.is_file())
    conflicts = [f for f in files if f != "note.md"]
    assert len(conflicts) == 2
    assert conflicts[0] != conflicts[1]


# ── Edge cases ────────────────────────────────────────────────────────


def test_missing_local_file_returns_auto_resolved(
    resolver: ConflictResolver, vault: Path
) -> None:
    result = resolver.resolve("nonexistent.md", {}, {})
    assert result == AUTO_RESOLVED
    assert not any(vault.iterdir())


def test_conflict_copy_metadata_preserved(
    resolver: ConflictResolver, vault: Path
) -> None:
    import os as _os

    p = _write(vault, "note.md", b"xyz")
    custom_mtime = 1700000000.0
    _os.utime(p, (custom_mtime, custom_mtime))

    resolver.resolve("note.md", {}, {})

    files = [f for f in vault.iterdir() if f.is_file()]
    conflict = next(f for f in files if f.name != "note.md")
    assert abs(conflict.stat().st_mtime - custom_mtime) < 1.0


def test_unicode_filename(resolver: ConflictResolver, vault: Path) -> None:
    _write(vault, "메모/오늘.md", "한국어")
    result = resolver.resolve("메모/오늘.md", {}, {})
    assert result == CONFLICT_CREATED

    names = sorted(p.name for p in (vault / "메모").iterdir())
    assert "오늘.md" in names
    conflict_name = next(n for n in names if n != "오늘.md")
    assert ".sync-conflict-" in conflict_name
