"""루트로 잘못 떨어진 파일 정리.

배경:
    Changes API 경로 해석 버그(수정됨)로 인해 서브폴더에 있어야 할 파일이
    볼트 루트로 복사되고, 그 결과 `.conflict-...` 사본까지 루트에 생성된
    사례가 있다. 이 스크립트는 아래 조건을 만족하는 루트 항목을 찾아낸다:

    1) state에서 루트에 있는 항목(경로에 "/" 없음)
    2) 같은 `drive_id`가 볼트 내 다른 서브폴더 경로에도 기록되어 있음
       → 루트 사본으로 판정
    3) (선택) 파일명이 `.conflict-` 패턴이면 루트 사본으로 간주

기본은 dry-run. 실제 삭제는 --apply 플래그. Drive 원본은 건드리지 않는다.

사용:
    uv run python scripts/cleanup_stray_root_files.py                 # dry-run
    uv run python scripts/cleanup_stray_root_files.py --apply         # 적용
    uv run python scripts/cleanup_stray_root_files.py --apply --trash # trash로 이동
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (uv run으로 직접 호출 대비)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 콘솔/파이프에서 한글 파일명 출력이 cp949로 깨지는 것 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from src.config import SyncConfig, load_config  # noqa: E402

CONFLICT_PATTERN = re.compile(r"\.conflict-[^.]+-\d{8}-\d{6}\.")


def is_root_path(rel: str) -> bool:
    """볼트 루트 바로 아래 파일인지 판정 (하위 폴더 없음)."""
    return "/" not in rel and "\\" not in rel


def is_conflict_name(rel: str) -> bool:
    return bool(CONFLICT_PATTERN.search(rel))


def find_candidates(state_data: dict) -> list[dict]:
    """정리 후보 목록을 계산한다.

    반환:
        [{"path": str, "drive_id": str | None, "reason": str,
          "original_path": str | None}, ...]

    판정 기준:
        1) conflict-copy-at-root: 루트에 `.conflict-...` 사본 이름
        2) duplicate-of-subfolder-original: 같은 drive_id 원본이 서브폴더에 있음
        3) samename-in-subfolder: 같은 파일명(basename)이 서브폴더에 있음
           — Changes API 경로 해석 버그로 drive_id가 다른 별도 복제가 생긴 사례
    """
    files: dict[str, dict] = state_data.get("files", {})
    # drive_id → [paths] 역인덱스
    by_drive_id: dict[str, list[str]] = {}
    # basename → [paths] 역인덱스 (서브폴더 원본 탐지용)
    by_basename: dict[str, list[str]] = {}
    for path, entry in files.items():
        drive_id = entry.get("drive_id")
        if drive_id:
            by_drive_id.setdefault(drive_id, []).append(path)
        basename = path.rsplit("/", 1)[-1]
        by_basename.setdefault(basename, []).append(path)

    candidates: list[dict] = []
    for path, entry in files.items():
        if not is_root_path(path):
            continue
        # (1) conflict 사본 이름이면 바로 후보 (루트에 있으면 잘못 만들어진 것)
        if is_conflict_name(path):
            candidates.append({
                "path": path,
                "drive_id": entry.get("drive_id"),
                "reason": "conflict-copy-at-root",
                "original_path": None,
            })
            continue
        drive_id = entry.get("drive_id")
        # (2) 같은 drive_id가 서브폴더에도 있으면 루트 쪽이 중복 사본
        if drive_id:
            subfolder_originals = [
                p
                for p in by_drive_id.get(drive_id, [])
                if p != path and not is_root_path(p)
            ]
            if subfolder_originals:
                candidates.append({
                    "path": path,
                    "drive_id": drive_id,
                    "reason": "duplicate-of-subfolder-original",
                    "original_path": subfolder_originals[0],
                })
                continue
        # (3) 파일명이 서브폴더에 있으면 (drive_id는 다르더라도) 루트 복제 의심
        basename = path
        same_basename_subfolder = [
            p
            for p in by_basename.get(basename, [])
            if p != path and not is_root_path(p)
        ]
        if same_basename_subfolder:
            candidates.append({
                "path": path,
                "drive_id": drive_id,
                "reason": "samename-in-subfolder",
                "original_path": same_basename_subfolder[0],
            })

    return candidates


def apply_cleanup(
    candidates: list[dict],
    state_path: Path,
    vault_path: Path,
    *,
    use_trash: bool,
) -> tuple[int, int]:
    """후보를 실제로 정리한다.

    - 로컬 파일: trash/로 이동(use_trash=True) 또는 unlink
    - state: 해당 항목 제거

    반환: (삭제된 파일 수, state에서 제거된 항목 수)
    """
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    files: dict[str, dict] = state_data.get("files", {})

    trash_dir = vault_path / ".sync" / "trash" / "cleanup_stray_root"
    if use_trash:
        trash_dir.mkdir(parents=True, exist_ok=True)

    removed_files = 0
    removed_state = 0
    for cand in candidates:
        rel = cand["path"]
        local_path = vault_path / rel
        if local_path.exists():
            if use_trash:
                dest = trash_dir / rel.replace("/", "__")
                shutil.move(str(local_path), str(dest))
            else:
                try:
                    local_path.unlink()
                except OSError:
                    continue
            removed_files += 1
        if rel in files:
            del files[rel]
            removed_state += 1

    state_data["files"] = files
    # 원자적 교체
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(state_path)
    return removed_files, removed_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="SyncConfig yaml 경로 (기본: config.yaml)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제 정리 수행. 기본은 dry-run.",
    )
    parser.add_argument(
        "--trash",
        action="store_true",
        help="로컬 파일을 unlink 대신 .sync/trash/cleanup_stray_root/ 로 이동.",
    )
    args = parser.parse_args()

    config: SyncConfig = load_config(Path(args.config))
    vault_path: Path = config.vault_path
    state_path: Path = vault_path / ".sync" / "sync_state.json"
    if not state_path.exists():
        print(f"[ERROR] state 파일이 없다: {state_path}", file=sys.stderr)
        return 2

    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    candidates = find_candidates(state_data)

    print(f"볼트: {vault_path}")
    print(f"state: {state_path}")
    print(f"후보 {len(candidates)}건:")
    for cand in candidates:
        exists_flag = "O" if (vault_path / cand["path"]).exists() else "X"
        orig = cand.get("original_path") or "-"
        print(
            f"  [{exists_flag}] {cand['path']}  "
            f"(reason={cand['reason']}, drive_id={cand['drive_id']}, "
            f"original={orig})"
        )

    if not args.apply:
        print("\n[DRY-RUN] --apply 없이 실행됨. 실제 파일/state 변경 없음.")
        return 0

    removed_files, removed_state = apply_cleanup(
        candidates, state_path, vault_path, use_trash=args.trash,
    )
    print(
        f"\n[APPLIED] 삭제된 파일 {removed_files}개, state 제거 {removed_state}개"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
