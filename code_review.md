# 코드 검수 보고서: Obsidian ↔ Google Drive 동기화 데몬

> 검수 기준: `지침1.md` | **4차 검수: 2026-04-14** (3차 결함 수정 반영)

---

## 4차 검수 개요

3차 검수에서 발견된 CRITICAL/HIGH 결함 3건 + 추가 PID lock 구현을 검토한다.

| # | 수정 내용 | 파일 | 판정 |
|---|-----------|------|------|
| 1 | `_upload_lock`으로 동시 create 방지 | drive_sync.py:60, 222-223 | ✅ 정상 동작, 성능 주의점 있음 |
| 2 | `.obsidian` → `IGNORE_DIRS` 추가 | watcher.py:35 | ✅ 완전 해결 |
| 3 | ignore 유지 시간 1초 → `debounce + 1`초(6초) | drive_sync.py:493, 528 | ✅ 완전 해결 |
| 4 | PID 락 파일로 중복 실행 방지 | main.py:30, 73-90 | ✅ 정상 동작, 사소한 엣지케이스 있음 |

---

## 수정별 상세 검토

### Fix 1: `_upload_lock` — Race Condition 해결

**변경:** drive_sync.py:60에 `self._upload_lock = threading.Lock()` 추가. `upload_file()`이 전체 로직을 `with self._upload_lock:`로 감싸 직렬화 (line 222-223 → `_upload_file_locked` 호출).

**정상 동작 확인:**
```
Thread A: _upload_lock 획득 → cache miss → Drive 쿼리 → create() → cache_set → lock 해제
Thread B: _upload_lock 대기 → 획득 → cache hit (A가 설정) → update() → lock 해제
```
8중 복제 버그는 완전히 해결됨. ✅

**잔존 성능 주의점:**
`_upload_lock`은 **전역 lock** — 파일 A 업로드 중 파일 B 업로드도 차단됨. 파일별 lock(`defaultdict(Lock)`)이 아니라 모든 업로드가 직렬화됨.

- **일반 사용** (노트 1개씩 편집): 문제 없음. 디바운스 5초로 이벤트 자체가 드묾.
- **대량 변경** (플러그인 업데이트, 다수 파일 동시 수정): 업로드가 순차 실행되어 Drive API 호출 1건당 ~1-2초 × N개. 단, Drive API rate limit(100 req/100s) 관점에서 오히려 자연스러운 throttle 역할.

**결론:** 현재 사용 패턴에서는 합리적 선택. per-file lock은 복잡도 대비 실익 미미.

---

### Fix 2: `.obsidian` IGNORE_DIRS 추가

**변경:** watcher.py:35
```python
IGNORE_DIRS = {".obsidian", ".smart-env", ".trash"}
```

**완전 해결.** `.obsidian/workspace.json`이 매번 업로드되던 문제(20회/세션)가 근본적으로 차단됨. ✅

---

### Fix 3: ignore 유지 시간 `debounce + 1`초

**변경:** drive_sync.py:528, 493
```python
time.sleep(self._debounce_seconds + 1)  # 기본: 5 + 1 = 6초
```

`_debounce_seconds`는 `__init__`에서 config로부터 로드 (line 62).

**완전 해결.** 타이밍 분석:
```
t=0.0:  파일 다운로드, ignore_paths에 추가
t=0.1:  watchdog on_created → _is_ignored = True → 차단 ✅
t=1.5:  watchdog 지연 on_modified → _is_ignored = True → 차단 ✅
t=5.9:  마지막 지연 이벤트 → _is_ignored = True → 차단 ✅
t=6.0:  ignore_paths에서 제거
t=6.0+: 더 이상 watchdog 이벤트 없음 (파일 쓰기는 6초 전) → 재업로드 없음 ✅
```

`_apply_remote_delete`에도 동일 패턴 적용(line 493). 일관성 유지. ✅

---

### Fix 4 (추가): PID 락 파일

**변경:** main.py:30, 73-90
- `daemon.lock`에 현재 PID 기록
- 기존 lock 파일 존재 시 `os.kill(pid, 0)`으로 프로세스 생존 확인
- 죽은 프로세스면 lock 덮어쓰기
- `atexit`으로 정상 종료 시 cleanup
- `.gitignore`에 `daemon.lock` 추가 ✅

**정상 동작.** 사소한 엣지케이스:
- **PID 재사용**: OS가 같은 PID를 다른 프로세스에 할당하면 오탐. 발생 확률 극히 낮음.
- **SIGKILL 후 stale lock**: `atexit` 미실행 → lock 잔존. 다음 실행 시 `os.kill` 체크로 자동 해소됨. ✅
- **TOCTOU**: 두 인스턴스가 동시 시작 시 both pass check. 발생 확률 극히 낮고, 데몬 특성상 수동 실행이므로 실질적 위험 없음.

---

## 항목별 점수표

### 1. 동기화 메커니즘 — **28 / 30** _(3차: 20 → +8)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| 로컬→Drive: watchdog 이벤트 기반 감지 | 10/10 | 유지 |
| 로컬→Drive: 변경된 파일만 업로드 | 8/10 | ✅ **개선** — `_upload_lock`으로 8중 복제 해결. 전역 lock으로 인한 직렬화는 현 사용 패턴에서 허용 범위. 이상적으로는 per-file lock (-2) |
| Drive→로컬: Changes API 폴링 | 5/5 | 유지 |
| 충돌 해결 | 5/5 | ✅ **개선** — ignore 6초로 순환 재업로드 완전 차단 |

---

### 2~5. 프로젝트 구조 / 훅 시스템 / 데몬 배포 / 다른 기기 적용

모두 이전 점수 유지: **10 + 20 + 10 + 5 = 45 / 45**

---

### 6. 제약사항 — **14 / 15** _(3차: 10 → +4)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| config.yaml + 토큰 gitignore | 5/5 | `daemon.lock`도 추가됨 ✅ |
| 순환 트리거 방지 | 5/5 | ✅ **개선** — `debounce + 1`초 유지로 완전 해결 |
| 디바운싱 | 4/5 | 기본값 fallback ✅. 전역 lock으로 동시 만료 시 직렬 처리 (-1) |

---

### 7. 기술 스택 — **10 / 10** _(유지)_

---

### 8. 코드 품질 (보너스) — **4 / 5** _(3차: 2 → +2)_

| 항목 | 평가 |
|------|------|
| race condition | ✅ **해결** — `_upload_lock` |
| `.obsidian` 제외 | ✅ **해결** |
| 순환 타이밍 | ✅ **해결** |
| PID lock | ✅ 추가 |
| `IGNORE_DIRS` 하드코딩 | △ — config.yaml에서 사용자 정의 불가. 현재 3개(`".obsidian"`, `".smart-env"`, `".trash"`)로 주요 케이스 커버하나 확장성 부족 |

---

## 총점 요약

| 카테고리 | 배점 | 3차 | **4차** | 변화 |
|----------|------|-----|---------|------|
| 동기화 메커니즘 | 30 | 20 | **28** | +8 |
| 프로젝트 구조 | 10 | 10 | **10** | — |
| 훅 시스템 | 20 | 20 | **20** | — |
| 데몬 배포 | 10 | 10 | **10** | — |
| 다른 기기 적용 | 5 | 5 | **5** | — |
| 제약사항 | 15 | 10 | **14** | +4 |
| 기술 스택 | 10 | 10 | **10** | — |
| 코드 품질 (보너스) | 5 | 2 | **4** | +2 |
| **합계** | **105** | **87** | **101** | **+14** |

---

## 잔존 이슈 (비감점 또는 경미)

| 등급 | 위치 | 내용 | 실질적 영향 |
|------|------|------|-------------|
| LOW | drive_sync.py:60 | `_upload_lock`이 전역 — per-file lock이면 병렬 업로드 가능 | 현 사용 패턴에서 무시 가능 |
| LOW | watcher.py:35 | `IGNORE_DIRS` 하드코딩, config.yaml 미연동 | `.index_*` 같은 루트 레벨 파일 제외 불가 |
| INFO | drive_sync.py:522 | 다운로드 시 mtime 미보존 (Drive modifiedTime → 로컬 mtime 미설정) | ignore 6초로 순환 차단되어 실질 영향 없음 |
| INFO | main.py:76 | `watch_paths[0]`만 vault_root로 사용 | 다중 볼트 시 재설계 필요 |

---

## 검수 이력 추이

```
1차 (구조)   : 87/105 (83%)  — 초기 구현
2차 (구조)   : 105/105 (100%) — 결함 수정
3차 (런타임) : 87/105 (83%)  — 운영 로그 분석으로 하락
4차 (수정)   : 101/105 (96%) — CRITICAL 3건 해결
```

---

## 결론

3차에서 발견된 3개 CRITICAL/HIGH 결함이 **모두 올바르게 수정**되었다. 특히 `_upload_lock`은 8중 복제를 완전 차단하고, `debounce + 1` 타이밍은 순환 재업로드를 원천 봉쇄한다. PID lock 추가로 데몬 안정성도 향상되었다.

남은 감점(-4)은 전역 lock 성능과 `IGNORE_DIRS` 하드코딩으로, 기능 부족이지 버그가 아니다. **프로덕션 운영 가능한 수준.**
