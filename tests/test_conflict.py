"""conflict.py 테스트.

충돌 사본 생성 규칙, 동일 타임스탬프 중복 처리, 확장자 없는 파일 처리를 검증한다.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.conflict import AUTO_RESOLVED, CONFLICT_CREATED, ConflictResolver

# ── 공용 fixture ──────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """임시 볼트 디렉토리를 제공한다."""
    return tmp_path


@pytest.fixture
def resolver(vault: Path) -> ConflictResolver:
    return ConflictResolver(device_id="my_pc", vault_path=vault)


def _write(vault: Path, rel: str, content: bytes | str = b"hello") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)
    return p


# ── 기본 생성 케이스 ──────────────────────────────────────────────────────


def test_creates_conflict_copy_in_same_directory(
    resolver: ConflictResolver, vault: Path
) -> None:
    _write(vault, "daily/note.md", b"local content")

    fixed_time = 1713100000.0  # 2024-04-14 UTC; strftime은 로컬 시간대 사용
    with patch("src.conflict.time.time", return_value=fixed_time):
        result = resolver.resolve(
            "daily/note.md",
            local_info={"mtime": 1.0, "size": 13},
            remote_info={"file_id": "remote_id", "modified_time": "2026-04-14T15:30:00Z"},
        )

    assert result == CONFLICT_CREATED

    # 사본 이름 규칙: 같은 폴더 + .conflict-my_pc-YYYYMMDD-HHMMSS.md
    siblings = list((vault / "daily").iterdir())
    names = sorted(p.name for p in siblings)
    assert len(names) == 2
    assert "note.md" in names
    conflict_name = next(n for n in names if n != "note.md")
    assert conflict_name.startswith("note.conflict-my_pc-")
    assert conflict_name.endswith(".md")

    # 사본은 로컬 원본과 동일한 내용
    conflict_path = vault / "daily" / conflict_name
    assert conflict_path.read_bytes() == b"local content"


def test_preserves_root_level_file(
    resolver: ConflictResolver, vault: Path
) -> None:
    _write(vault, "root.md", b"x")
    result = resolver.resolve("root.md", {}, {})
    assert result == CONFLICT_CREATED

    conflict_files = [p for p in vault.iterdir() if p.name != "root.md" and p.is_file()]
    assert len(conflict_files) == 1
    assert conflict_files[0].name.startswith("root.conflict-my_pc-")
    assert conflict_files[0].name.endswith(".md")


def test_file_without_extension(
    resolver: ConflictResolver, vault: Path
) -> None:
    _write(vault, "notes/README", b"readme")
    result = resolver.resolve("notes/README", {}, {})
    assert result == CONFLICT_CREATED

    names = sorted(p.name for p in (vault / "notes").iterdir())
    assert "README" in names
    conflict_name = next(n for n in names if n != "README")
    # 확장자가 없으므로 끝이 timestamp로 끝남 (원본 확장자 .md 같은 것 없음)
    assert conflict_name.startswith("README.conflict-my_pc-")
    # 마지막 6자리가 HHMMSS로 끝나야 함 (원본 확장자 미부착 확인)
    tail = conflict_name.rsplit("-", 1)[-1]
    assert len(tail) == 6 and tail.isdigit()


# ── 중복 처리 ────────────────────────────────────────────────────────────


def test_same_second_creates_unique_names(
    resolver: ConflictResolver, vault: Path
) -> None:
    """같은 초에 두 번 충돌이 발생해도 사본 이름이 유일해야 한다."""
    _write(vault, "note.md", b"a")

    fixed_time = 1713100000.0
    with patch("src.conflict.time.time", return_value=fixed_time):
        first = resolver.resolve("note.md", {}, {})
        # 두 번째 호출 시에도 time.time이 같게 고정 → 이름 중복 조건 의도적으로 발생
        second = resolver.resolve("note.md", {}, {})

    assert first == CONFLICT_CREATED
    assert second == CONFLICT_CREATED

    # 결과: 원본 + 사본 2개
    files = [p.name for p in vault.iterdir() if p.is_file()]
    assert len(files) == 3
    conflict_names = sorted(n for n in files if n != "note.md")
    assert len(conflict_names) == 2
    assert conflict_names[0] != conflict_names[1]


def test_one_second_apart_creates_distinct_names(
    resolver: ConflictResolver, vault: Path
) -> None:
    """1초 간격이면 기본 규칙만으로도 이름이 달라져야 한다."""
    _write(vault, "note.md", b"a")

    with patch("src.conflict.time.time", return_value=1713100000.0):
        resolver.resolve("note.md", {}, {})
    with patch("src.conflict.time.time", return_value=1713100001.0):
        resolver.resolve("note.md", {}, {})

    files = sorted(p.name for p in vault.iterdir() if p.is_file())
    conflicts = [f for f in files if f != "note.md"]
    assert len(conflicts) == 2
    assert conflicts[0] != conflicts[1]


# ── 엣지 케이스 ──────────────────────────────────────────────────────────


def test_missing_local_file_returns_auto_resolved(
    resolver: ConflictResolver, vault: Path
) -> None:
    """로컬 파일이 이미 사라진 상태 → 보존할 게 없으므로 AUTO_RESOLVED."""
    result = resolver.resolve("nonexistent.md", {}, {})
    assert result == AUTO_RESOLVED
    assert not any(vault.iterdir())


def test_conflict_copy_metadata_preserved(
    resolver: ConflictResolver, vault: Path
) -> None:
    """shutil.copy2는 mtime도 복사 → 원본과 사본의 stat이 같아야 한다."""
    p = _write(vault, "note.md", b"xyz")
    # 인위적으로 mtime 고정
    custom_mtime = 1700000000.0
    import os as _os

    _os.utime(p, (custom_mtime, custom_mtime))

    resolver.resolve("note.md", {}, {})

    files = [f for f in vault.iterdir() if f.is_file()]
    conflict = next(f for f in files if f.name != "note.md")
    assert abs(conflict.stat().st_mtime - custom_mtime) < 1.0


def test_unicode_filename(
    resolver: ConflictResolver, vault: Path
) -> None:
    """한국어 파일명에서도 정상 동작해야 한다."""
    _write(vault, "메모/오늘.md", "한국어")
    result = resolver.resolve("메모/오늘.md", {}, {})
    assert result == CONFLICT_CREATED

    names = sorted(p.name for p in (vault / "메모").iterdir())
    assert "오늘.md" in names
    conflict_name = next(n for n in names if n != "오늘.md")
    assert conflict_name.startswith("오늘.conflict-my_pc-")


def test_device_id_in_conflict_name(
    vault: Path,
) -> None:
    """device_id가 사본 이름에 포함되어야 한다."""
    _write(vault, "note.md", b"x")
    other = ConflictResolver(device_id="galaxy_s25", vault_path=vault)
    other.resolve("note.md", {}, {})

    conflicts = [p.name for p in vault.iterdir() if p.name != "note.md"]
    assert any("galaxy_s25" in n for n in conflicts)


def test_timestamp_format(
    resolver: ConflictResolver, vault: Path
) -> None:
    """YYYYMMDD-HHMMSS 형식인지 확인한다 (14자리 숫자 + 하이픈)."""
    _write(vault, "note.md", b"x")
    resolver.resolve("note.md", {}, {})

    conflict = next(p for p in vault.iterdir() if p.name != "note.md")
    # 예: note.conflict-my_pc-20260414-153000.md
    body = conflict.stem  # "note.conflict-my_pc-20260414-153000"
    parts = body.rsplit("-", 2)
    # 마지막 두 파트는 YYYYMMDD, HHMMSS
    assert len(parts) == 3
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 6 and parts[2].isdigit()
