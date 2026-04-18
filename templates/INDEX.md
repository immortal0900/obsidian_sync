# Templates Index

> Planner가 프로젝트에 필요한 템플릿을 선택할 때 참고하는 목차.
> 파일을 추가/수정하면 이 INDEX.md도 함께 업데이트할 것.

## 도메인 스펙 템플릿

| 파일 | 도메인 | 키워드 | 생성 산출물 |
|------|--------|--------|-------------|
| langgraph-agent.md | LangGraph 멀티 에이전트 | langgraph, state, node, edge, ReAct | artifacts/specs/langgraph-state.md |
| db-setup.md | 데이터베이스 | postgres, mysql, mongodb, sqlite, vector | artifacts/specs/db-schema.md |
| deepeval-setup.md | LLM 평가 | deepeval, metric, g-eval, test case | artifacts/specs/deepeval-metrics.md |
| langfuse-setup.md | LLM 추적/관측 | langfuse, trace, span, observability | artifacts/specs/langfuse-tracing.md |
| claude-cli-subprocess.md | Claude CLI 자동화/하네스 | claude, subprocess, headless, cli, agent, permission | artifacts/specs/claude-cli-integration.md |
| gdrive-watchdog-sync.md | Google Drive ↔ 로컬 양방향 sync | google drive, watchdog, bidirectional, echo loop, page token, multi-root, --config | artifacts/specs/gdrive-sync.md |

## 공통 템플릿

| 파일 | 용도 |
|------|------|
| sprint-contract-template.md | Sprint Contract 형식 (frontmatter 필수) |
| generator-guide.md | Generator 상세 가이드 참조 |
| journal-guide.md | `forge journal`이 작성하는 저널 양식·톤·링크 규칙 |

## 사용 규칙

- Planner는 먼저 이 INDEX를 읽고 후보를 선정한다
- 각 템플릿 파일 상단의 YAML frontmatter에서 상세 정보를 확인한다
- 프로젝트에 필요 없는 템플릿은 읽지 않는다 (토큰 절약)
- 새로운 도메인이 필요하면 기존 템플릿 형식을 따라 작성 후 이 INDEX에 추가

## fallback 원칙

**INDEX.md에 매칭되는 템플릿이 없어도 specs/*.md 생성을 건너뛰지 않는다.**
- 템플릿 없이 spec.md 내용만으로 specs/*.md를 직접 작성
- 파일명은 도메인 키워드 기반 자유 결정 (예: specs/auth-flow.md)
- 최소 포함 섹션: 목적, 주요 컴포넌트/인터페이스, 제약, 성공 기준
