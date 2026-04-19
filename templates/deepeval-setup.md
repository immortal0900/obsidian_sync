---
template_id: deepeval-setup
domain: LLM 평가
keywords: [deepeval, metric, g-eval, test case, evaluation, faithfulness, relevancy]
when_to_use: |
  LLM 출력 품질을 자동 평가하는 파이프라인이 필요할 때.
  DeepEval 메트릭과 테스트케이스 설계.
output: artifacts/specs/deepeval-metrics.md
related_templates: [langfuse-setup]
---

# DeepEval 평가 스펙 템플릿

## 메트릭
- (예: AnswerRelevancy, Faithfulness, ContextualPrecision)
- 임계값

## 테스트케이스
- 입력 / 기대 출력 / 컨텍스트
- 최소 N개

## 실행
- `deepeval test run tests/eval/`
- CI 연동 여부

## 검증
- 모든 메트릭 임계값 통과
- 실패 케이스 근거 로그 저장
