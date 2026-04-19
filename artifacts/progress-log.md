# Progress Log

## Sprint 4 — Session 1 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 + DoD 전체 완료 (100%)

### 완료한 작업

1. **P0-1: intent_log.py 구현** (`d78972f`)
   - `src/intent_log.py` 신규: JSONL append-only WAL
   - record(action) → UUID 반환, resolve(intent_id), replay(execute_fn), compact()
   - os.fsync 기반 내구성 보장
   - `tests/test_intent_log.py`: 14 tests — record/resolve 왕복, SIGKILL 시뮬레이션, replay 실패 처리, compact 크기 감소, corrupt line 방어

2. **P0-2: convergence.py 구현** (`69b631b`)
   - `src/convergence.py` 신규: ConvergenceManager + ConvergenceState
   - report_seen, check_converged, gc_eligible, blacklist_device
   - optimistic concurrency: exponential backoff + jitter (0.5s~8s, 6회 재시도)
   - `tests/test_convergence.py`: 16 tests — 단일/2기기 수렴, blacklist, gc_eligible, etag 경합, 재시도 실패

3. **P0-3: sync_engine.py Intent Log 통합** (`f185a37`)
   - `_run_action` 전 intent_log.record(), 성공 후 resolve()
   - `replay_intents()` 부트 시 미해결 intent 재실행
   - intent_log는 optional (기존 테스트 backward compatible)
   - `tests/test_sync_engine.py`: 4개 신규 테스트 — record/resolve 순서, 실패 시 resolve 미발생, 부트 replay, intent_log 없이 동작

4. **P0-4: main.py wiring + config 완성** (`3aafdde`)
   - main.py: IntentLog + ConvergenceManager 인스턴스 생성, SyncEngine에 intent_log 주입
   - run_app: state.load() 후 intent_log.replay() 호출
   - config.py: `tombstone_retention_days: int = 90` 추가
   - from_yaml(): `hash_max_file_size_mb`, `hash_verification`, `tombstone_retention_days` YAML 파싱 추가
   - `tests/test_config.py`: 2개 신규 테스트 — YAML 파싱 + 기본값 검증

5. **P0-5: 누적 QA 권고사항 해소** (`8c7aae0`)
   - (a) `REMOTE_PSEUDO_DEVICE = "_remote_"` 상수 정의 → 매직 스트링 제거
   - (b) `decide()`: md5 비교에 `not local.deleted and not remote.deleted` 가드 추가
   - (c) `run_without_state` non-empty version 분기 3개 테스트 추가 (upload/download/conflict)
   - (d) `VersionVector.__bool__` 명시: empty → falsy, `reconciler.py` falsy 판정 명시화
   - `ruff check` 통과

### 최종 테스트 결과

- `pytest tests/`: **454 passed, 2 skipped** (0 failures)
- `ruff check src/ tests/`: **All checks passed!**

### 내린 결정과 이유

- **IntentLog에서 os.fsync 사용**: JSONL append 후 즉시 fsync하여 SIGKILL 시에도 기록이 손실되지 않도록 보장
- **ConvergenceManager에 read_fn/write_fn 콜백 패턴**: Drive API 의존성을 역전하여 테스트 가능성 확보. 실제 Drive 연결은 인증 후 wiring
- **convergence 변수 noqa 처리**: ConvergenceManager는 초기화만 하고 tombstone GC 루프에서 사용 예정. 현재는 인스턴��만 생���

### 미처리 이슈

- ConvergenceManager의 Drive API 콜백 wiring 미완 — 실제 Drive 파일 읽기/쓰기 연결 필요 (tombstone GC 루프 구현 시 처리)
- 에코 억제 전용 테스트, device_id prefix 충돌 ��성 테스트 — P1 비차단
- `_do_conflict`에서 winner 필드 활용 분기 미구현 — P1 비차단

### 다음 세션에서 해야 할 것

1. **Evaluator 실행**: Sprint 4 QA → PASS 판정
2. **프로젝트 완성**: spec.md PR 로드맵 마지막 단계 완료. 이후 통합 테스트 환경에서 수동 검증 권장

---

## Sprint 3 — Session 1 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 + DoD 전체 완료 (100%)

### 완료한 작업

1. **P0-4: conflict.py Syncthing 명명 규칙** (`07a70b5`)
   - 파일명 포맷 변경: `{stem}.conflict-{device_id}-{ts}.{ext}` → `{stem}.sync-conflict-{ts}-{device_prefix}.{ext}`
   - device_prefix = device_id[:8] 사용
   - tests/test_conflict.py 9개 테스트 전부 통과

2. **P0-2 + P0-3: reconciler.py 전면 재작성 + run_without_state 재설계** (`0382915`)
   - 16셀 classify/decide 매트릭스 제거 → VectorOrdering 기반 `decide()` 구현
   - `decide_download_or_delete()`, `decide_upload_or_delete()` 보조 함수 구현
   - `resolve_conflict()` HLC tiebreaker + device prefix fallback 구현
   - Action 타입 추가: `AbsorbRemoteTombstone`, `UpdateVectorOnly`, `DeleteLocal`, `DeleteRemote` 등
   - `run_without_state()` md5 기반 5개 분기 구현:
     - Branch 1: 양쪽 md5 동일 → vector merge, 전송 없음
     - Branch 2: 로컬 only → upload
     - Branch 3: 원격 only → download
     - Branch 4: state 손실 + md5 불일치 → 강제 Conflict (P0 1-B 방어)
     - Branch 5: tombstone only → state에 deleted=True 흡수
   - `state.diff()` deprecated 처리
   - IGNORE_PATTERNS를 run_without_state Drive 목록 및 _classify_remote에도 적용
   - 기존 test_reconciler.py 20개 → version compare 호환으로 업데이트
   - 신규 test_reconciler_v2.py 29개 테스트 추가 (decide, resolve_conflict, run_without_state, 증상 3·4)

3. **P0-5: sync_engine md5 통합 + config 필드** (`c251521`)
   - `_do_upload`: `compute_md5()` → appProperties 및 FileEntry.md5에 기록
   - `_do_download`: `compute_md5()` → 다운로드 후 로컬 md5 계산하여 저장
   - `config.py`에 `hash_max_file_size_mb: int = 100`, `hash_verification: bool = True` 추가
   - Drive 메타데이터만 변경(md5 동일) 시 download skip (UpdateVectorOnly)

### 내린 결정과 이유

- **incremental run()에서 local deleted + no remote change 처리**: local이 삭제되고 remote change가 없을 때, old_files에서 remote entry를 복원하여 decide()에 전달. 이를 통해 DeleteRemote 액션 정상 생성.
- **conflict.py에서 device_id 전체 대신 prefix(8자) 사용**: Syncthing BEP 명세 준수 + 파일명 길이 최소화.
- **Google Docs (md5Checksum=None) 대응**: Drive md5가 없어도 local compute_md5()가 채워주므로 FileEntry.md5는 항상 유효. 기존 테스트 업데이트.

### 미처리 이슈

- Sprint 1/2 QA 권고사항(에코 억제 테스트, prefix 충돌 양성 테스트, trash_retention_days config 테스트)은 P1 비차단으로 미구현
- `_do_conflict`에서 winner 판정 로직은 reconciler의 conflict action에 "winner" 필드로 전달되나, sync_engine이 이를 활용하는 분기는 아직 미구현 (기존 동작: 항상 remote wins + conflict copy)

### 다음 세션에서 해야 할 것

1. **Evaluator 실행**: Sprint 3 QA → PASS 판정
2. **Sprint 4 착수**: PR4 — Intent Log WAL + Convergence 프로토콜 + 설정 완성

---

## Sprint 2 — Session 1 (2026-04-19)

**Sprint Contract 진행률**: 5/5 P0 + DoD 전체 완료 (100%)

### 완료한 작업

1. **P0-1: drive_vv_codec 구현** (`11a68df`)
   - `src/drive_vv_codec.py` 신규: encode/decode 왕복 변환, trim(28), legacy fallback
   - `tests/test_drive_vv_codec.py`: 29 tests — 왕복, trim, 바이트 실측, 엣지 케이스

2. **P0-2: drive_client.py 확장** (`e97fcbd`)
   - upload 시그니처에 `app_properties` kwarg 추가, 반환값 str→dict 변경
   - `download` 반환값에 메타데이터(md5Checksum, appProperties) 포함
   - `delete()` → `hard_delete()` rename
   - 신규: `ensure_tombstones_folder()`, `move_to_tombstones()`
   - `list_all_files()` fields에 `md5Checksum`, `appProperties` 추가
   - `get_changes()` fields에 `appProperties` 추가
   - `_normalize_change`에서 tombstones 폴더 parents 감지 → removed=True
   - 기존 테스트 전면 업데이트 + 신규 tombstone 테스트 4개

3. **P0-3: sync_engine.py 통합** (`eadda52`)
   - `_do_upload`: vv_encode → appProperties에 version vector 전달, Drive md5Checksum 저장
   - `_do_download`: vv_decode → 원격 vector를 로컬 version에 반영 (Sprint 1 QA -1점 해소)
   - `_do_delete_remote`: hard_delete → move_to_tombstones 교체 + appProperties 갱신
   - e2e_smoke, integration_watcher_poller 테스트 mock 업데이트

4. **P0-4: tombstones 폴더 방어** (`374ac36`)
   - `main.py`: 부트 시 `ensure_tombstones_folder()` 호출 + WARNING 로그
   - tombstones change detection 테스트 2개 추가

5. **P0-5: md5 조기 도입** (`9bc44a9`)
   - upload/download 시 Drive API md5Checksum → FileEntry.md5 기록
   - Google Docs(md5=None) graceful 처리 테스트 포함
   - download 시 원격 appProperties vector 반영 테스트

### 내린 결정과 이유

- **upload 반환값 str→dict 변경**: md5Checksum, appProperties를 받기 위해 필수. 기존 호출부 전면 업데이트.
- **_do_delete_remote에서 path 유무 분기**: path가 있으면 move_to_tombstones(vector 보존), 없으면 hard_delete fallback. 404 정리 경로와의 호환성 유지.
- **download에서 원격 vector 우선 적용**: remote_vv가 있으면 그대로 사용, legacy(empty)이면 로컬 갱신. 이로써 Sprint 1 QA의 `-1점` 감점 원인이 해소됨.

### 미처리 이슈

- Sprint 1 QA 권고사항(에코 억제 테스트, prefix 충돌 양성 테스트, trash_retention config 테스트)은 P1 비차단으로 이번 스프린트에서도 미구현. PR3/PR4에서 자연스럽게 추가 예정.

### 다음 세션에서 해야 할 것

1. **Evaluator 실행**: Sprint 2 QA → PASS 판정
2. **Sprint 3 착수**: PR3 — 3-way Reconciler 재설계 + 충돌 해결 + md5 로컬 계산

---

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
