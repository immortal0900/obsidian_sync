---
template_id: langgraph-agent
domain: LangGraph 멀티 에이전트
keywords: [langgraph, state, node, edge, react, reducer, send-api, conditional-edge]
when_to_use: |
  LangGraph 기반 멀티 에이전트 시스템을 구축할 때.
  특히 복수 에이전트가 병렬/순차로 상태를 공유하며 작업하는 경우.
output: artifacts/specs/langgraph-state.md
related_templates: [db-setup, langfuse-setup]
---

# LangGraph Agent 스펙 템플릿

## State 스키마
- TypedDict 또는 Pydantic BaseModel
- 필드: (나열)

## 노드
| 이름 | 입력 | 출력 | 책임 |
|------|------|------|------|
| analyze | state | state.analysis | ... |

## 엣지
- START → analyze
- analyze → (조건) → {action_a, action_b}
- * → END

## 검증
- 각 노드 단위 테스트 (입력→출력 단언)
- 전체 그래프 시나리오 테스트 2개 이상
- Langfuse trace 삽입 확인
