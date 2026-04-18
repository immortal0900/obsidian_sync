---
sprint_number: 4
has_next_sprint: false
estimated_remaining_sprints: 0
next_sprint_preview: |
  해당 없음. PR4가 spec.md PR 로드맵의 마지막 단계이며,
  이 스프린트 완료 시 Version Vector 기반 동기화 재설계가 완성된다.
---

# Sprint 4 Contract

**목표:** PR4 — Intent Log WAL로 부분 실패 복구를 보장하고, Convergence 프로토콜로 tombstone 안전 GC를 구현하며, 누적된 QA 권고사항과 설정 파싱 미비를 해소하여 프로젝트를 완성한다.
**예상 기간:** 1 세션 (2-3시간)

## 포함 범위 (P0)

- [x] **P0-1: intent_log.py 구현** — `src/intent_log.py` 신규. JSONL append-only WAL. `record(action)`: action 실행 전 append. `resolve(intent_id)`: 성공 후 resolved=true 기록. `replay(engine)`: 부트 시 미해결 intent 재실행. `compact()`: 해결된 항목 제거. 검증 기준: `pytest tests/test_intent_log.py` 전부 통과 — record/resolve 왕복, SIGKILL 시뮬레이션(record 후 resolve 없이 replay → 미해결 action 재실행), replay 실패 시 WARNING 로그, compact 후 파일 크기 감소.
  - 참조: specs/tombstone-convergence.md #2.4

- [x] **P0-2: convergence.py 구현** — `src/convergence.py` 신규. Drive `.sync/convergence.json` 읽기/쓰기. `report_seen(device_id, tombstone_ids)`: 자기 기기 확인 목록 업데이트. `check_converged(tombstone_id)`: 모든 활성 기기(blacklist 제외) 확인 여부. `gc_eligible(tombstone_id, now, retention_days=90)`: 수렴 + 90일 경과. `blacklist_device(device_id)`: 영구 오프라인 기기 제외. 경합 대응: optimistic concurrency with Drive `etag` 조건부 PATCH, 실패 시 exponential backoff + jitter (초기 0.5s, 배수 2, 최대 8s, 최대 6회 재시도). 검증 기준: `pytest tests/test_convergence.py` — 단일 기기 즉시 수렴, 2기기 양쪽 확인 후에만 gc_eligible=True, blacklist 제외, 90일 미경과 시 gc_eligible=False, 경합 재시도 성공.
  - 참조: specs/tombstone-convergence.md #2.3, spec.md §3.5

- [x] **P0-3: sync_engine.py Intent Log 통합** — `_run_action` 전 `intent_log.record(action)`, action 성공 후 `intent_log.resolve(intent_id)`. 시작 시 `intent_log.replay()` 호출. 검증 기준: sync_engine 테스트에서 intent record/resolve 호출 순서 검증 (mock), replay 통합 테스트.
  - 참조: spec.md §PR4 수정 파일 — sync_engine.py

- [x] **P0-4: main.py wiring + config 완성** — `main.py`에 IntentLog + ConvergenceManager 인스턴스 생성 및 SyncEngine 주입. `config.py`에 `tombstone_retention_days: int = 90` 추가. `from_yaml()`에서 `tombstone_retention_days`, `hash_max_file_size_mb`, `hash_verification`, `trash_retention_days`를 YAML로부터 파싱하도록 보완 (Sprint 3 QA 권고 #3). 검증 기준: config YAML에 해당 필드 설정 시 기본값 대신 사용자 값 적용 확인, main.py 부트 시 IntentLog/ConvergenceManager 정상 초기화.
  - 참조: specs/tombstone-convergence.md #2.3, #2.4, spec.md §PR4

- [x] **P0-5: 누적 QA 권고사항 해소** — Sprint 1/2/3 QA에서 반복 지적된 미해결 P1을 일괄 처리:
  (a) `_remote_` 매직 스트링 상수화 (`reconciler.py:319`) → `REMOTE_PSEUDO_DEVICE` 상수 정의.
  (b) `decide()` 에서 deleted 엔트리의 md5 비교 방어 (`reconciler.py:89-95`) → `not local.deleted and not remote.deleted` 조건 추가.
  (c) `run_without_state` non-empty version 분기 테스트 추가 (`reconciler.py:503-530`) — 커버리지 89% → 95%+.
  (d) `VersionVector.__bool__` 명시 또는 falsy 판정 코드 수정 (`reconciler.py:328`).
  검증 기준: (a-d) 각 항목별 테스트 존재 또는 코드 변경 확인 + `ruff check` 통과.
  - 참조: sprint-3-done.md 권고사항 #1-#4

## 제외 범위

- md5+size 기반 rename 최적화 (`on_moved` → 동일 content 감지) — spec.md에서 "후속 PR로 유보" 명시
- 영구 오프라인 기기 자동 감지 — spec.md에서 "초기 버전은 수동 blacklist" 명시
- 멀티 디바이스 E2E 시뮬레이션 (spec.md §6.3) — 통합 테스트 환경 제약, 수동 검증으로 대체

## 이전 스프린트 미해결 이슈

| # | 이슈 | 출처 | 이번 스프린트 처리 |
|---|------|------|--------------------|
| 1 | 에코 억제 전용 테스트 미작성 | Sprint 1/2/3 QA | P1 — 가능하면 추가, 비차단 |
| 2 | device_id prefix 충돌 양성 테스트 미작성 | Sprint 1/2/3 QA | P1 — 가능하면 추가, 비차단 |
| 3 | config YAML 파싱 미연동 (hash/trash 설정) | Sprint 3 QA #3 | **P0-4에서 처리** |
| 4 | `_remote_` 매직 스트링 상수화 | Sprint 3 QA #1 | **P0-5(a)에서 처리** |
| 5 | deleted 엔트리 md5 비교 방어 | Sprint 3 QA #4 | **P0-5(b)에서 처리** |
| 6 | run_without_state non-empty version 분기 테스트 | Sprint 3 QA #2 | **P0-5(c)에서 처리** |
| 7 | VersionVector falsy 판정 명시화 | Sprint 3 QA #2 코드 품질 | **P0-5(d)에서 처리** |
| 8 | `_do_conflict` winner 판정 분기 미구현 | progress-log Sprint 3 | P1 — reconciler가 conflict action에 winner 필드를 전달하므로, sync_engine이 이를 활용하는 분기 추가 권장. 비차단 |

## Definition of Done

- [x] 모든 P0 체크박스 완료
- [x] `ruff check src/ tests/` 통과
- [x] `pytest tests/` 통과 (기존 테스트 회귀 0건)
- [x] 신규 파일 커버리지 ≥ 90% (`intent_log.py`, `convergence.py`)
- [x] Intent Log: record → SIGKILL 시뮬레이션 → replay 테스트 통과
- [x] Convergence: 단일/다중 기기 수렴 + blacklist + 90일 보존 테스트 통과
- [x] config YAML 파싱: `tombstone_retention_days`, `hash_max_file_size_mb`, `hash_verification`, `trash_retention_days` 모두 from_yaml()에서 읽힘
- [x] Sprint 3 QA 권고사항 4건(매직 스트링, deleted 방어, 커버리지, falsy) 해소
- [x] progress-log.md 업데이트
