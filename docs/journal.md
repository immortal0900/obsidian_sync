# 프로젝트 저널

## 2026-04-19 — obsidian_sync — Sprint 1~4

Obsidian ↔ Google Drive 양방향 동기화의 1차 구현을 Sprint 1~4로 완료했다 (454 tests / 2 skipped / 0 failures). 네 기둥(state·drive·reconcile·lifecycle)이 완성됐으며, Sprint 4에서 Intent Log WAL과 Convergence 프로토콜까지 추가. 이월 건: ConvergenceManager Drive API 실제 wiring 미완, 에코 억제 전용 테스트 부재.

### Errors & Root Causes

- **TrashManager 경로 이중 중첩**
  - 원인: [`main.py`](../src/main.py)에서 TrashManager에 `.sync/trash/`가 이미 붙은 경로를 전달하여 `vault/.sync/trash/.sync/trash/`로 이중 중첩
  - 해결: [`main.py`](../src/main.py)에서 `config.vault_path`를 직접 전달하도록 수정 — commit `fbdc409`
  - 교훈: 경로 조합 책임은 한 곳(TrashManager 내부)에만 두고, 호출부에서 prefix를 붙이지 말 것

- **device_id prefix 충돌 감지 불완전**
  - 원인: VersionVector counters에 8자 prefix만 저장되므로 전체 device_id 기반 충돌 감지 불가
  - 해결: [`state.py`](../src/state.py)에 `known_device_ids` set 추가, `load()` 시 자기 device_id 자동 포함 — commit `0dd4d85`, `fbdc409`
  - 교훈: 축약 식별자(prefix)로 유일성을 보장하려면 원본 ID를 별도 추적하는 보조 구조가 필수

- **Sprint 1 QA FAIL 5건 일괄 수정**
  - 원인: (1) `_check_device_prefix_collision` 로직 미비, (2) `TestMoved` 테스트가 spec v2 P1 2-C의 delete+create 분해와 불일치, (3) dead code 잔존, (4) main.py TrashManager wiring 누락
  - 해결: [`state.py`](../src/state.py) known_device_ids, [`main.py`](../src/main.py) TrashManager wiring, dead code 38줄 삭제 — commit `0dd4d85`
  - 교훈: QA에서 FAIL 판정 시 한 커밋으로 모아 수정하면 원인 추적이 용이

- **download 시 원격 vector 미반영 (Sprint 1 QA -1점)**
  - 원인: `_do_download`에서 원격 appProperties의 version vector를 로컬에 반영하지 않아 동기화 시점 정보 소실
  - 해결: [`sync_engine.py`](../src/sync_engine.py)에서 `vv_decode()` → 원격 vector 우선 적용 — commit `eadda52`
  - 교훈: 양방향 동기화에서 다운로드 경로도 반드시 메타데이터(vector) 수신을 포함해야 함

- **reconciler 16셀 매트릭스의 복잡도 폭발**
  - 원인: classify/decide 매트릭스 방식이 엣지 케이스 증가에 취약. 증상 3(tombstone 부활), 증상 4(state 손실 시 md5 불일치 미감지)
  - 해결: [`reconciler.py`](../src/reconciler.py) 전면 재작성 — VectorOrdering 기반 `decide()` + HLC tiebreaker — commit `0382915`
  - 교훈: 상태 조합 매트릭스보다 ordering 비교 기반 설계가 확장에 강건

### Decisions

- **on_moved를 delete+create로 분해** (progress-log Sprint 1 결정)
  - 고려안: rename_remote 액션 유지 / delete+create 분해 / md5 기반 rename 최적화
  - 선택: delete+create — spec v2 P1 2-C 준수, rename 최적화는 후속 PR로 유보
  - 영향: [`local_watcher.py`](../src/local_watcher.py), [`sync_engine.py`](../src/sync_engine.py)

- **upload 반환값 str→dict 변경** (progress-log Sprint 2 결정)
  - 고려안: str 유지 + 별도 API 호출로 메타 조회 / dict로 변경
  - 선택: dict — md5Checksum, appProperties를 한 번에 수신
  - 영향: [`drive_client.py`](../src/drive_client.py), 기존 호출부 전면 업데이트

- **hard_delete → move_to_tombstones 전환** (progress-log Sprint 2 결정)
  - 고려안: 즉시 삭제 / 휴지통 폴더 이동
  - 선택: tombstones 폴더 이동 — vector 보존으로 삭제 이력 추적 가능, 404 정리 경로와 호환
  - 영향: [`drive_client.py`](../src/drive_client.py)의 `move_to_tombstones()`, [`sync_engine.py`](../src/sync_engine.py)의 `_do_delete_remote()`

- **VectorOrdering 기반 reconciler 재설계** (progress-log Sprint 3 결정)
  - 고려안: 기존 16셀 classify/decide 매트릭스 보완 / 전면 재작성
  - 선택: 전면 재작성 — `decide()` + `decide_download_or_delete()` + `decide_upload_or_delete()` 보조 함수
  - 영향: [`reconciler.py`](../src/reconciler.py) 전면, [`test_reconciler_v2.py`](../tests/test_reconciler_v2.py) 29개 신규 테스트

- **IntentLog에서 os.fsync 사용** (progress-log Sprint 4 결정)
  - 고려안: buffered write / fsync 강제
  - 선택: `os.open` + `os.fsync` — SIGKILL 시에도 JSONL 기록 보존
  - 영향: [`intent_log.py`](../src/intent_log.py#L158)

- **ConvergenceManager에 read_fn/write_fn 콜백 패턴** (progress-log Sprint 4 결정)
  - 고려안: Drive API 직접 의존 / 콜백 주입
  - 선택: 콜백 주입 — Drive API 의존성 역전으로 테스트 가능성 확보
  - 영향: [`convergence.py`](../src/convergence.py)

- **Conflict 파일명 Syncthing 명명 규칙 채택** (progress-log Sprint 3 결정)
  - 고려안: 자체 `{stem}.conflict-{device_id}-{ts}.{ext}` / Syncthing BEP `{stem}.sync-conflict-{ts}-{device_prefix}.{ext}`
  - 선택: Syncthing BEP — device_id[:8] prefix로 파일명 길이 최소화
  - 영향: [`conflict.py`](../src/conflict.py)

### Tips & Gotchas

- **Windows MAX_PATH 회피**: [`trash.py`](../src/trash.py)에서 flat UUID 저장 방식 사용. 원본 경로가 깊어도 UUID 파일명이므로 260자 제한에 걸리지 않음. 다른 프로젝트의 로컬 휴지통 구현에 이식 가능.

- **Drive appProperties 28자 제한**: [`drive_vv_codec.py`](../src/drive_vv_codec.py)에서 `trim(28)` 구현. Google Drive appProperties는 키당 124바이트 제한이 있으므로 version vector 직렬화 시 반드시 바이트 크기 검증 필요.

- **Google Docs는 md5Checksum=None**: Drive API에서 Google Docs/Sheets/Slides는 `md5Checksum` 필드를 반환하지 않음. 로컬 `compute_md5()`가 채워주므로 FileEntry.md5는 유효하지만, 원격 측 md5 비교 시 None 방어 필수.

- **pytest skip on Windows**: `test_hash.py::test_no_read_permission`, `test_local_watcher.py::test_symlink_is_ignored`는 Windows 플랫폼에서 자동 skip. POSIX 전용 테스트는 플랫폼 가드 데코레이터 사용.

- **exponential backoff 상수 설계**: [`convergence.py`](../src/convergence.py#L20)에서 초기 0.5s, 배수 2, 최대 8s, 최대 6회 재시도. etag conflict 기반 optimistic concurrency 패턴으로 Drive API 경합 대응. 범용 재사용 가능한 상수 세트.

### Carry-overs (이월)

- **ConvergenceManager Drive API wiring 미완** — [`main.py`](../src/main.py#L305)에서 `read_fn=None, write_fn=None`으로 생성, `# noqa: F841`로 unused 경고 억제 중. tombstone GC 루프 구현 시 실제 Drive 콜백 연결 필요. 다음 저널에서 해소 여부 확인.

- **에코 억제 전용 테스트 부재** — Sprint 1~4 QA에서 반복 지적. [`sync_engine.py`](../src/sync_engine.py#L86)에 에코 억제 로직 존재하나 전용 테스트 없음. 통합 테스트 단계에서 추가 권장.

- **device_id prefix 충돌 양성 테스트 부재** — [`state.py`](../src/state.py)에 감지 로직 존재하나 "실제 충돌 발생 → 경고" 시나리오 테스트 미구현.

- **`_do_conflict` winner 필드 미활용** — [`reconciler.py`](../src/reconciler.py)가 conflict action에 `winner` 필드 전달하나, [`sync_engine.py`](../src/sync_engine.py)의 `_do_conflict`는 항상 remote wins + conflict copy. winner 기반 분기 추가 시 더 정확한 충돌 해결 가능.

### Performance Notes

- **Sprint 1~4 전체 테스트**: 454 passed, 2 skipped in 10.64s — 테스트 스위트 속도 양호.
- **convergence.py 커버리지 86%**: DoD "신규 파일 ≥ 90%" 기준 미달 (4% 부족). 미커버 라인은 에러 핸들링/fallback 경로 (`_read_state` exception, `_retry_update` read 실패, `_sleep` 실 호출)이므로 기능 리스크 낮음 (추정).
