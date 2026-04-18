# Progress Log

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
