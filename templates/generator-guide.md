# Generator 상세 가이드

## 커밋 형식

**짧고 직관적인 영어**로. 허용 prefix는 세 가지만:

- `feat:` — 기능 추가
- `fix:` — 버그 수정
- `refactor:` — 동작 변경 없는 구조 개선

예:
```
feat: add watcher debounce
fix: handle empty vault path
refactor: extract sync_engine
```

**금지:**
- `test:`, `docs:`, `chore:` 등 다른 prefix 사용 금지 (위 3가지로 흡수)
- 한국어 커밋 메시지 금지
- `Co-Authored-By: Claude ...` 같은 자동 서명 금지
- 여러 줄 요약/본문 금지 (한 줄 요약 원칙, 필요시 본문 1줄만 "왜")

## progress-log.md 형식

매 세션 종료 시 맨 위에 다음 블록을 추가:

```
## {ISO timestamp} — sprint-{N}, session-{M}

### 완료
- feat: ... (commit: abc123)

### 진행 중
- (있으면)

### 결정
- 왜 A 대신 B를 선택했는가

### 미처리 이슈
- ...

### 다음 세션에서 (우선순위 순)
1. ...
2. ...
```

## 의사결정 기록 — decisions/decision-NNN.md

스펙 모순 또는 판단 필요 지점에서:

```
# Decision {NNN}: {제목}

## 맥락
## 옵션
## 선택
## 근거
## 미해결 질문 (사용자에게)
```

그 후 사용자에게 질문하라.
