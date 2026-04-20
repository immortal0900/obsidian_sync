"""Drive 볼트 루트의 잘못된 복제 파일 정리.

배경:
    Changes API 경로 해석 버그(수정됨)로 서브폴더에 있어야 할 파일이
    Drive 볼트 루트에 별도 drive_id로 복제 생성된 사례가 있다.
    로컬만 정리하면 서비스 재시작 시 Drive 루트의 복제가 다시 내려온다.

동작:
    1) DriveClient로 볼트 루트의 전체 파일 목록을 긁는다 (list_all_files).
    2) relative_path에 "/"가 없는(= 루트 직계) 파일을 찾는다.
    3) 같은 basename이 서브폴더에도 있으면 루트 쪽이 중복 사본.
    4) 기본은 dry-run. --apply면 Drive 휴지통으로 이동 (hard_delete).

Drive 휴지통은 30일 내 복구 가능하므로 비교적 안전하다.

사용:
    uv run python scripts/cleanup_drive_root_duplicates.py
    uv run python scripts/cleanup_drive_root_duplicates.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from src.config import SyncConfig, load_config  # noqa: E402
from src.drive_client import DriveClient  # noqa: E402


def find_candidates(all_files: list[dict]) -> list[dict]:
    """루트 직계 파일 중 서브폴더에 동명 파일이 있는 것을 후보로."""
    by_basename: dict[str, list[dict]] = {}
    for item in all_files:
        rel = item.get("relative_path") or item.get("name", "")
        basename = rel.rsplit("/", 1)[-1]
        by_basename.setdefault(basename, []).append(item)

    candidates: list[dict] = []
    for item in all_files:
        rel = item.get("relative_path") or item.get("name", "")
        # 루트 직계인지 (경로에 / 없음)
        if "/" in rel or "\\" in rel:
            continue
        same = by_basename.get(rel, [])
        # 같은 basename이면서 서브폴더에 있는 항목 존재 여부
        others_in_subfolder = [
            other
            for other in same
            if other.get("id") != item.get("id")
            and "/" in (other.get("relative_path") or "")
        ]
        if others_in_subfolder:
            candidates.append({
                "id": item["id"],
                "name": rel,
                "size": item.get("size"),
                "original_path": others_in_subfolder[0].get("relative_path"),
            })
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--apply", action="store_true", help="실제로 Drive 휴지통으로 이동."
    )
    args = parser.parse_args()

    config: SyncConfig = load_config(Path(args.config))
    drive = DriveClient(config)
    drive.authenticate()

    print("Drive 볼트 전체 목록 조회 중...")
    all_files = drive.list_all_files()
    candidates = find_candidates(all_files)

    print(f"루트 중복 후보 {len(candidates)}건:")
    for c in candidates:
        print(
            f"  - {c['name']} (id={c['id']}, size={c['size']}) "
            f"| subfolder original: {c['original_path']}"
        )

    if not args.apply:
        print("\n[DRY-RUN] --apply 없이 실행. Drive 변경 없음.")
        return 0

    moved = 0
    for c in candidates:
        try:
            drive.hard_delete(c["id"])
            moved += 1
        except Exception as e:
            print(f"  실패: {c['name']} ({c['id']}) -> {e}")
    print(f"\n[APPLIED] Drive 휴지통 이동 {moved}건 (30일 내 복구 가능)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
