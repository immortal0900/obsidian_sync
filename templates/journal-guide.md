# Journal 작성 가이드

`forge journal`이 호출하는 `journal` 서브에이전트가 참고하는 양식·톤·링크 규칙 가이드.
에이전트 본체 지시는 `.claude/agents/journal.md`에 있다. 이 파일은 **작성 예시와 실전 팁**.

## 엔트리 예시 (연속 스프린트)

```markdown
## 2026-04-17 — obsidian_sync — Sprint 1~4

Obsidian ↔ Google Drive 양방향 동기화의 1차 구현을 Sprint 1~4로 마무리 (269 tests / 93% 커버리지). 네 기둥(state·drive·reconcile·lifecycle) 완성. 이월 건: watcher 예외 모니터링 공백.

### Errors & Root Causes

- **Sprint 1 planner 세션이 ERROR로 종료**
  - 원인: claude-p 호출이 130초 경과 후 실패, 토큰 0 기록. 사유 로그 없음 (추정: 일시적 네트워크 문제)
  - 해결: 재실행 — Sprint 1 evaluator 단계부터는 정상
  - 교훈: planner 단독 실패는 산출물이 비는 형태로만 드러난다 — [harness-cost-log.txt](../artifacts/harness-cost-log.txt) 의 `ERROR` 상태를 매 스프린트 시작 전 확인

- **`(UNCHANGED × DELETED)` 셀 no-op이 유령 drive_id 남김**
  - 원인: spec의 "이미 없음" 문구를 문자 그대로 no-op으로 구현 → state의 `drive_id` 잔존 → 다음 로컬 수정 때 사라진 파일로 `upload(update)` 시도 → 404
  - 해결: [reconciler.py:287](../src/reconciler.py#L287) 에서 `self._state.remove_file(path)` 호출 — commit `4b75e11`
  - 교훈: "no-op" 셀도 state 정합성 유지를 위해 엔트리 제거까지는 수행해야 한다

### Decisions

- **404 정책: `DriveFileNotFoundError` + sync_engine 자동 정리** (근거: [decision-002.md](../artifacts/decisions/decision-002.md))
  - 고려안: A) HttpError 그대로 전파, B) drive_client 내부에서 state 직접 수정(계층 위반), C) 전용 예외 + 호출자가 정리
  - 선택: C — 의미론 명시, 정리 로직을 `sync_engine` 한 곳에 집중
  - 영향: [drive_client.py:39](../src/drive_client.py#L39), [sync_engine.py:82](../src/sync_engine.py#L82) — commit `b4737cb`

### Tips & Gotchas

- **atomic write는 `BaseException` 핸들러로 감싸라** — [state.py:203](../src/state.py#L203) 참고. `Exception`만 잡으면 `KeyboardInterrupt` 인터럽트에서 tmp 파일이 남는다
- **Windows signal 처리는 `loop.add_signal_handler` → `signal.signal` fallback** — [main.py:236](../src/main.py#L236) `install_signal_handlers`. `add_signal_handler`가 `NotImplementedError` 내면 전통 시그널로 전환

### Carry-overs

- **watcher Timer 스레드 예외 모니터링 공백** — [local_watcher.py](../src/local_watcher.py) `_fire_change`가 `logger.exception`로 삼키는데, 모니터링 연결은 Sprint 5 이후 과제

### Performance Notes

- **sprint-3 generator 세션 986.8s** — 4커밋을 한 세션에 담아 평균 대비 큼. 다음엔 "커밋 3~4개 = 1세션" 상한 가이드 적용
```

## 안 좋은 엔트리 예시 (피해야 할 것)

```markdown
### Errors & Root Causes

- 토큰이 0으로 나옴        # ❌ 증상만 있고 근본 원인/해결 없음
  - 뭔가 이상함              # ❌ 정보 없음
  - src/cost_tracker.py 에서 # ❌ 링크 아님, 라인 번호 없음
```

## 톤

- 짧고 단정적. "우리가 발견했다" 대신 "발견됐다".
- 미래 재발 방지를 목표로 — 미래의 자신이 검색할 키워드를 한 줄 요약에 포함.
- 비즈니스 로직 세부 (예: "userId가 null일 때 A 필드 대신 B 필드 쓴다")는 **프로젝트 내 스펙/decisions**에 남기고, journal에는 **범용 재사용 가능한 지식**만.

## 중복/덮어쓰기

- 기존 `docs/journal.md` 읽고, 동일 날짜/범위 엔트리가 이미 있으면 **기존을 업데이트**.
- 완전히 새 기간이면 **최상단에 새 엔트리 추가**.
- 오래된 엔트리는 절대 삭제/수정하지 마라.

## 링크 체크리스트

- [ ] 모든 파일 참조가 `[label](path)` 형태인가
- [ ] 함수 참조가 `()` 포함해 함수임을 명시하는가 (예: `apply_changes()`)
- [ ] 라인 번호 `#L42`를 붙였으면 실제 그 라인에 해당 심볼이 있는가
- [ ] 상대 경로가 `docs/journal.md` 기준으로 올바른가 (`../src/...`, `../artifacts/...`)

## 금지

- 코드나 artifacts/* 수정
- 평문 파일 경로 (링크 없이)
- 검증 없는 추측 (`아마도`, `대략`) — 확신 없으면 `(추정)` 꼬리표
- 프로젝트 밖 사용자 시스템 관련 내용
