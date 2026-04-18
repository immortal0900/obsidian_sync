# Progress Log

## Sprint 1 — Session 3 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 + DoD 전체 완료 (100%) — QA FAIL 수정 완료

### 완료한 작업

1. **QA FAIL 수정** (`fbdc409`)
   - `main.py`: TrashManager에 `config.vault_path`를 직접 전달 (이전: `.sync/trash/`를 이미 붙인 경로를 전달하여 이중 중첩)
   - `state.py`: `load()` 시 현재 `device_id`를 `known_device_ids`에 자동 추가 — prefix 충돌 감지 완전화

### 내린 결정과 이유

- `known_device_ids.add(self.device_id)`를 load() 직후에 배치: 첫 실행이든 기존 state든 자기 자신이 항상 set에 포함되어야 충돌 감지가 의미 있음

### 미처리 이슈

- 통합 테스트 8개 파일 의존성 미설치 (watchdog, googleapiclient) — 환경 문제, 코드 결함 아님

### 다음 세션에서 해야 할 것

1. **Evaluator 재실행**: QA 수정 확인 → PASS 판정
2. **Sprint 2 착수**: PR2 — Drive appProperties 통합 + Drive Tombstone 폴더

---

## Sprint 1 — Session 2 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 + DoD 전체 완료 (100%)

### 완료한 작업

1. **QA FAIL 5건 전체 수정** (`0dd4d85`)
   - `_check_device_prefix_collision` 로직 수정: `known_device_ids` set을 state에 추가하여 전체 device_id 기반 prefix 충돌 감지. VV counters의 prefix-only 한계 해소.
   - `TestMoved` 3개 테스트를 delete+create 분해에 맞게 재작성 (spec v2 P1 2-C 정합)
   - Dead code 제거: `_schedule_move`, `_fire_move`, `_MOVED_KEY_PREFIX` (38줄 삭제)
   - `main.py`에 TrashManager wiring: `build_context()`에서 TrashManager 인스턴스 생성 → SyncEngine 주입
   - `ruff check` 통과 확인 → DoD 체크박스 완료

### 내린 결정과 이유

- **known_device_ids를 state JSON에 추가**: VV counters에는 8자 prefix만 저장되므로 collision 감지가 원천적으로 불가능했음. 전체 device_id를 별도 추적하여 해결. PR2에서 원격 vector 수신 시 자동으로 채워질 예정.

### 미처리 이슈

- watchdog/googleapiclient 미설치 환경이므로 test_local_watcher.py, test_sync_engine.py 등 8개 통합 테스트 파일은 실행 불가. 전체 환경에서 검증 필요.

### 다음 세션에서 해야 할 것

1. **Evaluator 재실행**: QA 수정 확인 → PASS 판정
2. **Sprint 2 착수**: PR2 — Drive appProperties 통합 + Drive Tombstone 폴더

---

## Sprint 1 — Session 1 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 완료 (100%)

### 완료한 작업

1. **P0-1: VersionVector 핵심 구현** (`8e641e2`)
   - `src/version_vector.py` 신규 생성: empty/update/compare/merge/trim + HLC
   - `tests/test_version_vector.py`: 33 tests, 100% 커버리지
   
2. **P0-2: FileEntry v2 스키마 + State 마이그레이션** (`61f18fc`)
   - FileEntry에 version, deleted, deleted_at, md5 필드 추가
   - SyncState.VERSION = 2, v1→v2 자동 마이그레이션 + .v1.bak 백업
   - scan_local_files에서 기존 version 복사
   - 기존 테스트 v2 호환 업데이트 (e2e_smoke 포함)
   - 신규 테스트: FileEntryV2, V1ToV2Migration, ScanPreservesVersion (13 tests)

3. **P0-3: TrashManager 구현** (`b45c58a`)
   - `src/trash.py` 신규 생성: move/gc/list_entries/restore
   - Flat UUID 저장으로 Windows MAX_PATH 회피
   - `tests/test_trash.py`: 19 tests

4. **P0-4: sync_engine + local_watcher 통합** (`9fd9e56`)
   - sync_engine: 모든 _do_* 메서드에 version.update(device_id) 추가
   - _do_delete_local: TrashManager.move() 사용 (fallback: unlink)
   - _do_delete_remote: deleted=True tombstone 기록 (기존 remove_file 대신)
   - local_watcher: on_moved를 delete+create로 분해 (spec v2 P1 2-C)
   - 기존 sync_engine 테스트 업데이트 (delete → deleted=True 마킹)

5. **P0-5: config 확장 + prefix 충돌 감지** (`fae29b5`)
   - config.py: STATE_VERSION=2, DEFAULT_TRASH_RETENTION_DAYS=30, trash_retention_days 필드
   - state.py: _check_device_prefix_collision() 부트 시 실행

### 내린 결정과 이유

- **_do_delete_remote에서 remove_file 대신 update_file(deleted=True)**: spec.md §3.2에 따라 삭제도 vector 증분 이벤트로 취급. tombstone 기록이 부활 방지의 핵심.
- **on_moved를 delete+create 분해**: rename_remote 액션 유지 대신 delete+create로 확정 (spec v2 P1 2-C). md5+size 기반 rename 최적화는 후속 PR로 유보.
- **trash_manager를 optional param으로**: 기존 테스트 호환성을 위해 None일 때 fallback으로 unlink 사용.

### 미처리 이슈

- `ruff check`는 환경에 ruff 미설치로 검증 불가. Evaluator가 확인 필요.
- sync_engine/local_watcher/e2e_smoke 테스트는 `googleapiclient`/`watchdog` 의존성 미설치로 이 환경에서 실행 불가. 전체 환경에서 검증 필요.
- main.py에 TrashManager wiring은 아직 미구현 (P0-4 범위에 명시되지 않았으나 실제 실행 시 필요).

### 다음 세션에서 해야 할 것

1. **Evaluator QA 실행**: qa-report.md 생성 → FAIL 항목 수정
2. **main.py wiring**: TrashManager 인스턴스를 SyncEngine에 주입
3. **Sprint 2 착수**: PR2 — Drive appProperties 통합 + Drive Tombstone 폴더
