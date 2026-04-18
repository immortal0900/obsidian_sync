---
sprint_number: 2
has_next_sprint: true
estimated_remaining_sprints: 2
next_sprint_preview: |
  PR3 — 3-way Reconciler 재설계 + 충돌 해결 + md5 로컬 계산.
  decide() 함수를 version compare 기반으로 전면 재작성하고,
  Syncthing 충돌 명명 규칙 적용, run_without_state 재설계,
  증상 3·4 E2E 테스트로 부활 방지를 검증한다.
---

# Sprint 2 Contract

**목표:** Drive appProperties에 Version Vector를 저장/로드하고, 원격 삭제를 `.sync/tombstones/` move로 처리하여 양방향 vector 전파의 기반을 완성한다.
**예상 기간:** 2-3 세션

## 포함 범위 (P0)

- [x] **P0-1: drive_vv_codec 구현** — `src/drive_vv_codec.py` 신규 생성. `encode(VersionVector, deleted, md5) -> dict[str, str]`과 `decode(appProperties) -> (VersionVector, deleted, md5)` 왕복 변환. `trim(28)` 포함. `ot_sync_schema=v2` 없는 파일은 legacy(version=empty) 처리.
  - 검증 기준: `pytest tests/test_drive_vv_codec.py` 전부 통과. encode→decode 왕복 동일성, 30-key 초과 시 trim(28) 동작, legacy 파일 fallback, key+value ≤ 124바이트 실측 테스트 포함.
  - 참조: specs/gdrive-sync.md #2.3, specs/version-vector.md #2.1

- [x] **P0-2: drive_client.py 확장** — upload 시그니처에 `vector, deleted, md5` 추가하여 appProperties 전달. `list_all_files()` / `get_changes()` fields에 `appProperties`, `parents` 추가. 신규 메서드: `ensure_tombstones_folder()` (`.sync/tombstones/` 폴더 ID 반환, 없으면 생성), `move_to_tombstones(file_id)` (parents 변경 + deleted=1). 기존 `delete()`는 `hard_delete()`로 rename.
  - 검증 기준: upload 호출 시 appProperties dict가 API body에 포함되는 것을 mock 테스트로 확인. `ensure_tombstones_folder` 중복 호출 시 폴더 중복 생성 방지. `move_to_tombstones` 후 parents 변경 확인.
  - 참조: specs/gdrive-sync.md #2.1

- [x] **P0-3: sync_engine.py 통합** — `_do_upload`에서 drive_client.upload에 version 전달. `_do_download`에서 Drive 응답의 appProperties를 decode하여 로컬 version 설정 (Sprint 1의 로컬-only 갱신 교체, Sprint 1 QA 감점 해소). `_do_delete_remote`에서 `move_to_tombstones` 사용 (기존 delete 대체). `_change_to_action`에서 parents가 tombstones 폴더인 change를 `ACTION_DELETE_LOCAL`로 분류.
  - 검증 기준: `_do_upload` 후 state의 version이 drive에 전달된 것과 일치. `_do_download` 후 로컬 FileEntry.version이 원격 appProperties의 vector와 동일. delete_remote가 hard delete 대신 tombstone move를 호출. 기존 sync_engine 테스트 회귀 없음.
  - 참조: specs/gdrive-sync.md #2.1, spec.md §3.5

- [x] **P0-4: poller.py 확장 + tombstones 폴더 방어** — Changes API fields에 `parents`, `appProperties` 추가. parents 변경이 tombstones 폴더로의 move인 경우 삭제 이벤트로 분류. 부트 시 `.sync/tombstones/` 폴더 부재 감지 → WARNING 로그 + 자동 재생성.
  - 검증 기준: mock Changes 응답에 appProperties/parents 포함 시 정상 파싱. tombstones 폴더 부재 시 WARNING 로그 출력 + 재생성 호출 확인.
  - 참조: specs/gdrive-sync.md, spec.md §3.5 (P1 4-A)

- [x] **P0-5: md5 조기 도입 (Drive API md5Checksum 저장)** — upload/download 시 Drive API가 반환하는 `md5Checksum`을 `FileEntry.md5`에 기록 시작. 로컬 파일의 md5 직접 계산(`src/hash.py`)은 PR3 범위이므로 이번에는 Drive 응답값만 저장.
  - 검증 기준: upload 성공 후 state의 해당 FileEntry.md5가 Drive 응답의 md5Checksum과 일치. download 후에도 동일. md5가 None인 파일(Google Docs 등)은 graceful 처리.
  - 참조: specs/gdrive-sync.md #2.1 (P1 3-A 조기 도입)

## 제외 범위

- 로컬 파일 md5 직접 계산 (`src/hash.py`) — PR3
- reconciler.py 재작성 (3-way decide 로직) — PR3
- 충돌 해결 + `.sync-conflict-*` 명명 — PR3
- `run_without_state` 재설계 — PR3
- Intent Log / Convergence — PR4
- convergence 기반 hard delete 실행 — PR4

## 이전 스프린트 미해결 이슈

- **`_do_download` 원격 vector 미반영** (Sprint 1 QA 감점 -1) → P0-3에서 해결
- **에코 억제 전용 테스트 미작성** (Sprint 1 QA 권고 #1) — sync_engine 수정 시 함께 추가 권장 (P1, 비차단)
- **device_id prefix 충돌 양성 테스트 미작성** (Sprint 1 QA 권고 #2) — P1, 비차단
- **trash_retention_days config 전달 테스트 미작성** (Sprint 1 QA 권고 #3) — P1, 비차단

## Definition of Done

- [x] 모든 P0 체크박스 완료
- [x] `ruff check src/ tests/` 통과
- [x] `pytest tests/` 통과 (기존 336+ 테스트 회귀 없음)
- [x] 신규 파일 (`drive_vv_codec.py`) 커버리지 ≥ 90%
- [x] 수정 파일 (`drive_client.py`, `sync_engine.py`, `poller.py`) 관련 테스트 추가
- [x] appProperties key+value 바이트 실측 — `ot_sync_vv_<8자>` + `<13자리 ms>` = 32바이트 ≤ 124바이트 한도 내 확인
- [x] progress-log.md 업데이트
