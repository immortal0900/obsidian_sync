# Changelog

## 2.0.0 — 2026-04-19 — Version Vector 재설계

v1(mtime+size 기반 2-way 동기화)에서 발생한 4가지 증상을 근본 해결:

1. Drive에 파일 추가 → 로컬에 없으면 **지워짐** (기대: download)
2. 로컬에 파일 추가 → Drive에 없으면 **지워짐** (기대: upload)
3. 로컬에서 삭제해도 Drive에서 **다시 내려받음**
4. Drive에서 삭제해도 로컬에서 **다시 올라감**

### 변경 사항

- **Syncthing BEP 기반 Version Vector** 도입 — 파일마다 `{device_prefix → HLC counter}` 벡터 유지
- **HLC (Hybrid Logical Clock)** — `max(existing+1, unix_ms)` 로 시계 편차 방어 + 단조 증가 보장
- **3-way 판정** — `Equal / Greater / Lesser / Concurrent` 를 vector 비교로 결정
- **논리적 삭제** — 실삭제 대신 `.sync/tombstones/`(Drive) 및 `.sync/trash/`(로컬) 이동으로 tombstone 보존
- **Intent Log WAL** — `_do_delete_remote` 같은 action의 부분 실패 복구 보장
- **Convergence 프로토콜** — 모든 활성 기기가 tombstone 확인 후 90일 경과 시에만 hard delete
- **충돌 명명 규칙** — `.conflict-*` → `.sync-conflict-<YYYYMMDD-HHMMSS>-<device_prefix>.<ext>` (Syncthing 호환)
- **md5 기반 content match** — mtime만 바뀌고 내용 동일하면 전송 생략

### 신규 모듈

`src/version_vector.py`, `src/drive_vv_codec.py`, `src/trash.py`, `src/convergence.py`, `src/intent_log.py`, `src/hash.py`

### 제거/교체

- v1의 "REMOTE_DELETED + LOCAL_UNCHANGED → 보존" 정책 제거 (Syncthing [PR #10207](https://github.com/syncthing/syncthing/pull/10207) 교훈 반영)
- v1의 `run_without_state` 경로 비교 기반 fallback → tombstone-aware 병합 모드로 재작성
- Concurrent tiebreaker를 wall-clock `mtime`에서 HLC `max(counters.values())` 로 교체

### 마이그레이션

- `sync_state.json` v1 → v2 자동 변환. 기존 파일은 `version=empty, deleted=False` 로 시작.
- v1 백업은 `sync_state.json.v1.bak` 으로 자동 보관 (다운그레이드 안전망).

### 테스트

- 458 passed, 2 skipped, 0 failures
- `ruff check src/ tests/` 통과

### 상세

- 설계 문서: [docs/architecture/sync-design.md](docs/architecture/sync-design.md)
- 엔지니어링 저널: [docs/journal.md](docs/journal.md)
- 스프린트 아카이브: `artifacts/sprint-1~4-done.md`

## 0.1.0 — 초기 (사전 릴리스)

mtime+size 기반 초기 2-way 동기화 구현. v2.0에서 재설계됨.
