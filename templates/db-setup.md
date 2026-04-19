---
template_id: db-setup
domain: 데이터베이스
keywords: [postgres, mysql, mongodb, sqlite, vector, schema, migration, index]
when_to_use: |
  데이터베이스 스키마 설계, 인덱스, 벡터 검색, 마이그레이션이 필요할 때.
output: artifacts/specs/db-schema.md
related_templates: []
---

# DB 설정 스펙 템플릿

## 스키마
- 테이블: (나열)
- PK/FK
- 인덱스 (쿼리 패턴 기반)

## 벡터 검색 (해당 시)
- 임베딩 모델, 차원
- 거리 함수 (cosine/l2/ip)
- ANN 인덱스 (HNSW, IVFFlat)

## 마이그레이션
- 도구 (alembic 등)
- 롤백 정책

## 검증
- 스키마 DDL 적용
- 시드 데이터 입력 후 CRUD 테스트
