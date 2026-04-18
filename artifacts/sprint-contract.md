---
sprint_number: 1
has_next_sprint: true
estimated_remaining_sprints: 3
next_sprint_preview: |
  PR2 — Drive appProperties 통합 + Drive Tombstone 폴더.
  drive_vv_codec.py 신규 구현, drive_client.py에 appProperties/tombstone 메서드 추가,
  poller.py Changes API fields 확장, sync_engine.py 원격 vector 전달 통합.
  PR2 완료 후 Integration Gate(실제 Drive 왕복 실측) 수행.
---

# Sprint 1 Contract

**목표:** Version Vector(HLC) 로컬 도입 + FileEntry v2 마이그레이션 + 로컬 Tombstone(`.sync/trash/`) 구현
**예상 기간:** 1일 (집중 세션 기준)

## 포함 범위 (P0)

- [x] **P0-1: VersionVector 핵심 구현** — `src/version_vector.py` 신규 생성. `empty()`, `update(device_id, now?)`, `compare(other)`, `merge(other)`, `trim(max_devices=28)` 구현.
  - 검증 기준: `pytest tests/test_version_vector.py -v` 전부 통과. HLC strict increase, 시간 역행 방어, 5가지 VectorOrdering, trim(28) 동작 100% 커버리지.
  - 참조: specs/version-vector.md §2.1, §2.6

- [x] **P0-2: FileEntry v2 스키마 + State 마이그레이션** — `src/state.py`에 `version`, `deleted`, `deleted_at`, `md5` 필드 추가. `load()`에 v1→v2 자동 마이그레이션(기존 entry는 `version=empty, deleted=False`). `save()`에 신규 필드 직렬화. v1 백업(`sync_state.json.v1.bak`) 생성. `src/config.py`에 `STATE_VERSION = 2` 추가.
  - 검증 기준: `pytest tests/test_state.py -v` 통과. v1 JSON → load → v2 FileEntry 변환 왕복, to_dict → from_dict 동일성, 기존 테스트 회귀 없음.
  - 참조: specs/version-vector.md §2.3

- [x] **P0-3: TrashManager 구현** — `src/trash.py` 신규 생성. `.sync/trash/{uuid}` flat 저장 + `{uuid}.json` 메타데이터. `move(abs_path, rel_path)`, `gc(now, retention_days=30)`, `list_entries()`, `restore(uuid, target_path)` 구현.
  - 검증 기준: `pytest tests/test_trash.py -v` 전부 통과. move 후 파일+메타 존재, gc 30일 경과 항목만 삭제, `should_ignore('.sync/trash/foo')` true 확인.
  - 참조: specs/tombstone-convergence.md §2.1

- [x] **P0-4: sync_engine + local_watcher 통합** — `src/sync_engine.py`에서 `_do_upload`/`_do_download`/`_do_delete_remote`/`_do_delete_local` 직후 `version.update(device_id)` 호출. `_do_delete_local`은 파일을 `trash_manager.move()`로 이동. `src/local_watcher.py`에 `on_moved` 이벤트 바인딩 + delete+create 분해. trash 이동 시 에코 억제(`_mark_local_written`).
  - 검증 기준: 로컬 삭제 통합 테스트(파일 삭제 → trash 이동 확인), rename 통합 테스트(`on_moved` → old path `deleted=True` + new path `empty.update(dev)`), 에코 억제 검증. `pytest` 관련 테스트 전부 통과.
  - 참조: specs/version-vector.md §2.4, specs/tombstone-convergence.md §2.1

- [x] **P0-5: config 확장 + 수동 검증** — `src/config.py`에 `trash_retention_days: int = 30` 추가. device_id prefix(8자) 충돌 감지 시 WARNING 로그. 수동 검증 스크립트 실행: 파일 생성→version 확인, 수정→value 증가, 삭제→trash 이동.
  - 검증 기준: `ruff check` 통과, `pytest` 전체 통과, 수동 검증 시나리오 3건(생성/수정/삭제) 정상 동작.
  - 참조: spec.md §PR1 수동 검증

## 제외 범위

- Drive appProperties 통합 (PR2 범위)
- `src/drive_vv_codec.py`, `src/drive_client.py` 변경 (PR2)
- `src/hash.py` 로컬 md5 계산 (PR3 범위, md5 필드는 PR1에서 선언만)
- Reconciler 전면 재설계 (PR3)
- Intent Log, Convergence 프로토콜 (PR4)
- 증상 3·4 E2E 테스트 (PR3에서 reconciler 재설계 후 검증)

## 이전 스프린트 미해결 이슈

- 해당 없음 (첫 스프린트)

## Definition of Done

- [x] 모든 P0 체크박스 완료
- [x] `ruff check` 통과
- [x] `pytest` 통과 (신규 파일 커버리지 ≥ 90%)
- [x] 기존 테스트 회귀 없음 (v2 마이그레이션으로 인한 깨짐 0건)
- [x] progress-log.md 업데이트
