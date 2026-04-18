---
sprint_number: 3
has_next_sprint: true
estimated_remaining_sprints: 1
next_sprint_preview: |
  PR4 — Intent Log WAL + Convergence 프로토콜 + 설정 완성.
  src/intent_log.py: JSONL WAL record/resolve/replay/compact.
  src/convergence.py: .sync/convergence.json 읽기/쓰기, 수렴 판정, hard delete GC.
  main.py IntentLog + ConvergenceManager wiring, tombstone_retention_days=90.
---

# Sprint 3 Contract

**목표:** PR3 — reconciler.py를 version compare 기반으로 전면 재작성하고, Syncthing 충돌 규칙·md5 로컬 해싱·run_without_state 재설계를 완성하여 증상 1~4를 근본 해결한다.
**예상 기간:** 1 세션 (2-3시간)

## 포함 범위 (P0)

- [x] **P0-1: hash.py 구현** — `src/hash.py` 신규. 청크 단위 md5 계산, `hash_max_file_size_mb` (기본 100MB) 초과 시 None 반환. 검증 기준: `pytest tests/test_hash.py` 전부 통과. 엣지 케이스: 빈 파일, 100MB 초과 파일, 읽기 권한 없는 파일.
  - 참조: specs/version-vector.md #2.3 (FileEntry.md5 필드), specs/gdrive-sync.md #2.1 (PR2 md5 조기 도입과의 관계)

- [x] **P0-2: reconciler.py 전면 재작성** — spec.md §3.3의 `decide()` / `decide_download_or_delete()` / `decide_upload_or_delete()` / `resolve_conflict()` 구현. 기존 `_classify_local/_classify_remote` 16셀 규칙 제거 → version compare 기반 판정으로 교체. `state.py`의 `diff()` deprecated 처리 또는 제거. 검증 기준: `pytest tests/test_reconciler_v2.py` — Equal/Greater/Lesser/ConcurrentGreater/ConcurrentLesser 각 시나리오 + tombstone 흡수(`AbsorbRemoteTombstone`) 분기 + md5+size 동일 시 `UpdateVectorOnly` 검증.
  - 참조: specs/version-vector.md #2.5, #2.6

- [x] **P0-3: run_without_state 재설계** — spec.md §3.7의 5개 분기 구현. 핵심: `local.version == empty + md5 불일치 → 강제 Conflict` (P0 1-B 데이터 유실 방어). Drive `files.list`로 전체 파일 + `.sync/tombstones/` 내용 스캔 후 path별 비교. 검증 기준: (1) 양쪽 md5 동일 → 전송 없음 + vector merge, (2) 로컬 unique → upload, (3) 원격 unique → download, (4) state 손실 후 로컬 편집본 존재 + 원격 다른 내용 → `.sync-conflict-*`로 로컬 보존 + 원격 download, (5) tombstone만 존재 → 로컬 state에 `deleted=True` 기록.
  - 참조: specs/version-vector.md #2.5a

- [x] **P0-4: conflict.py Syncthing 명명 규칙 + resolve_conflict HLC tiebreaker** — 파일명 포맷 변경: `{stem}.conflict-{device_id}-{ts}.{ext}` → `{stem}.sync-conflict-{ts}-{device_prefix}.{ext}`. `resolve_conflict`의 HLC tiebreaker 구현: (1) `max(version.counters.values())` 비교 — 큰 쪽 승리, (2) HLC 동률 시 device prefix 큰 쪽 패배, (3) 패배 측을 conflict copy로 보존. 검증 기준: `pytest tests/test_conflict.py` — Concurrent 시 HLC 큰 쪽 승, HLC 동률→prefix 판정, mtime 조작(과거/미래)해도 HLC가 승패 결정.
  - 참조: specs/version-vector.md #2.6, spec.md §3.3 resolve_conflict 의사코드

- [x] **P0-5: sync_engine md5 통합 + IGNORE_PATTERNS 확장 + 증상 3·4 E2E** — `_do_upload/_do_download` 성공 후 `hash.compute_md5()` → `FileEntry.md5` 기록 (Drive md5Checksum과 병행). `config.py`에 `hash_max_file_size_mb: int = 100`, `hash_verification: bool = True` 추가. IGNORE_PATTERNS를 `run_without_state` Drive 목록 필터 및 `_classify_remote` Changes 결과에도 적용 (specs/gdrive-sync.md #2.5의 누락 3개 지점). Drive 메타데이터만 변경(md5 동일, modifiedTime 다름) 시 download skip. 증상 3·4 E2E 테스트 작성/보강.
  - 참조: specs/gdrive-sync.md #2.5, spec.md §6.2 시나리오 3·4

## 제외 범위

- Intent Log (`src/intent_log.py`) — PR4 범위
- Convergence 프로토콜 (`src/convergence.py`) — PR4 범위
- `tombstone_retention_days` 설정 — PR4 범위
- main.py IntentLog/ConvergenceManager wiring — PR4 범위
- md5+size 기반 rename 최적화 (`on_moved` → 동일 content 감지) — 후속 PR로 유보

## 이전 스프린트 미해결 이슈

| # | 이슈 | 출처 | 이번 스프린트 처리 |
|---|------|------|--------------------|
| 1 | 에코 억제 전용 테스트 미작성 | Sprint 1/2 QA 권고 #1 | P1 — 가능하면 추가, 비차단 |
| 2 | device_id prefix 충돌 양성 테스트 미작성 | Sprint 1/2 QA 권고 #2 | P1 — 가능하면 추가, 비차단 |
| 3 | trash_retention_days config 전달 테스트 미작성 | Sprint 1/2 QA 권고 #3 | P1 — 가능하면 추가, 비차단 |
| 4 | `state.py diff()` deprecated 처리 | spec.md PR3 체크리스트 | P0-2에서 처리 |

## Definition of Done

- [x] 모든 P0 체크박스 완료
- [x] `ruff check src/ tests/` 통과
- [x] `pytest tests/` 통과 (기존 테스트 회귀 0건)
- [x] 신규 파일 커버리지 ≥ 90% (`hash.py`, `reconciler.py` 재작성 부분)
- [x] `decide()` 단위 테스트: 5가지 VectorOrdering 각각 + tombstone 흡수 + md5 동일 시나리오
- [x] `run_without_state` 5개 분기 각각 테스트
- [x] 증상 3·4 E2E 테스트 통과 (삭제 후 재시작 → 부활 없음)
- [x] `resolve_conflict` HLC tiebreaker + device prefix fallback 테스트
- [x] progress-log.md 업데이트
