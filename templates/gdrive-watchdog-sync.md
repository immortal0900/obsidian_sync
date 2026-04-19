---
template_id: gdrive-watchdog-sync
domain: Google Drive ↔ 로컬 양방향 동기화
keywords: [google drive, watchdog, sync, bidirectional, page token, changes api, echo loop, multi-root, --config]
when_to_use: |
  로컬 파일 시스템(watchdog/fsevents/inotify)과 Google Drive API v3를
  양방향으로 동기화하는 데몬/서비스를 구현할 때.
  노트 앱 벌트, 문서 폴더 미러링, 프로젝트 디렉터리 자동 백업,
  한 기기에서 여러 로컬 폴더를 각각 다른 Drive 폴더와 동기화하는 경우 등.
output: artifacts/specs/gdrive-sync.md
related_templates: []
---

# Google Drive ↔ 로컬 watchdog 양방향 동기화 — 시행착오 방지 가이드

> 실제 양방향 sync 데몬을 운영하며 마주친 **반복적인 버그·함정·해결책**을 정리한 문서.
> 새 프로젝트에서 같은 함정을 다시 밟지 않도록 참조한다.
>
> 용어 정리:
> - **sync root** (= "동기화 루트") — watchdog으로 감시하는 로컬 루트 폴더 하나
> - **remote root** — Drive에서 대응되는 폴더 (folder_id 로 지정)
> - **앱별 캐시** — 어떤 앱이 자기 데이터용으로 쓰는 폴더/파일. 동기화 대상에 보통 포함하면 안 됨

---

## 1. 에코 루프 — 양방향 sync의 1순위 버그

### 증상
동기화 데몬을 켜두면 **같은 파일이 계속 download → upload → download**로 순환한다.
로그에 동일한 경로가 수 초 간격으로 반복되며 CPU·네트워크가 무한 소비된다.

### 근본 원인
양방향 sync는 **자기 자신이 만든 쓰기 이벤트에 반응**하기 쉽다:

1. **다운로드 에코** (Drive → Local → Drive):
   ```
   Drive에서 A 파일 다운로드 → 로컬 파일 시스템에 씀
   → watchdog의 on_modified 이벤트 발생
   → 업로드 큐에 적재 → Drive에 업로드 → 사이클 반복
   ```

2. **업로드 에코** (Local → Drive → Local):
   ```
   로컬 A 편집 → watchdog 감지 → Drive에 업로드
   → Drive Changes API가 이 변경을 change로 보고
   → Poller가 "Drive에 변경이 있다" → 다운로드 큐에 적재 → 사이클 반복
   ```

### 해결책: 에코 억제 (Echo Suppression)

엔진에 두 개의 단기 캐시를 유지한다:

```python
class SyncEngine:
    ECHO_SUPPRESS_WINDOW_SECONDS = 15.0

    def __init__(self, ...):
        self._recent_local_writes: dict[str, float] = {}   # path → 만료 monotonic
        self._recent_drive_writes: dict[str, float] = {}   # drive_id → 만료 monotonic

    def _mark_local_written(self, path: str) -> None:
        self._recent_local_writes[path] = (
            time.monotonic() + self.ECHO_SUPPRESS_WINDOW_SECONDS
        )

    def _mark_drive_written(self, drive_id: str) -> None:
        self._recent_drive_writes[drive_id] = (
            time.monotonic() + self.ECHO_SUPPRESS_WINDOW_SECONDS
        )

    def _is_echo_local(self, path: str) -> bool:
        deadline = self._recent_local_writes.get(path)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._recent_local_writes.pop(path, None)
            return False
        return True

    def handle_local_change(self, event_type, path):
        if self._is_echo_local(path):
            return  # 우리가 방금 쓴 파일의 watcher 이벤트 → 무시
        # ... 정상 처리

    def _change_to_action(self, change):
        if self._is_echo_drive(change["file_id"]):
            return None  # 우리가 방금 업로드한 drive_id의 변경 → 무시
        # ... 정상 처리
```

**적용 지점:**
| 작업 | 호출할 mark |
|---|---|
| 다운로드 실행 직전 | `_mark_local_written(path)` |
| 로컬 삭제 실행 직전 | `_mark_local_written(path)` |
| 업로드 완료 후 | `_mark_drive_written(drive_id)` |
| 충돌 처리 중 다운로드 | `_mark_local_written(path)` |

**윈도우 크기:** 15초가 watcher 디바운스(2s) + 큐 처리(~10s) 여유를 커버. 길면 적법한 외부 변경도 무시될 위험. 짧으면 에코를 놓침.

### 주의: 에코 억제로도 못 잡는 케이스

**초기 reconcile가 긴 경우**: cold start 시 500개 액션을 30분 걸려 처리하면, 처음 업로드한 drive_id는 이미 15초 만료 후 poller가 시작됨. Poller가 get_changes()로 "내가 올린" 파일들을 change로 수신 → 다운로드 큐잉.

**완화 방법** (선택):
- Poller 시작 전에 `get_initial_token()`을 한 번 더 호출해서 최신 토큰으로 갱신 (reconcile 중 생긴 변경을 스킵)
- 또는 md5/size 비교로 "내용이 정말 달라졌나" 체크 후 download

---

## 2. IGNORE_PATTERNS는 양쪽에 동시 적용해야 한다

### 증상
앱별 캐시 폴더나 시스템 파일 패턴이 로컬 스캔에서는 잘 걸러지는데,
**Drive에서 내려오는 파일은 그대로 다운로드**된다.

### 근본 원인
필터가 로컬 경로에만 적용되고, 다음 세 지점에 적용이 빠짐:

1. `reconciler.run_without_state()` — `drive.list_all_files()` 결과 필터 안 함
2. `reconciler._classify_remote()` — `drive.get_changes()` 결과 필터 안 함
3. `sync_engine._change_to_action()` — poller 경로 필터 안 함

### 해결책

```python
# reconciler.run_without_state()
remote_by_path = {
    item["relative_path"]: item
    for item in remote_files
    if not should_ignore(item["relative_path"])  # ← 추가
}

# reconciler._classify_remote()
if known_path is not None:
    if should_ignore(known_path):
        continue
    # ...
else:
    name = file_meta.get("name")
    if should_ignore(name):
        continue
    # ...

# sync_engine._change_to_action()
if existing_path is not None:
    if should_ignore(existing_path):
        return None
    # ...
if should_ignore(name):
    return None
```

**원칙**: `should_ignore()`는 **Drive API 응답을 받는 모든 지점**에서 호출해야 한다.
- 로컬 scan (기본)
- Drive list_all_files 결과
- Drive changes API 결과 (reconciler + poller 양쪽)
- watcher 이벤트 (기본)

---

## 3. 글로브 패턴은 경로 전체 구성요소에 매칭해야 한다

### 증상
IGNORE_PATTERNS에 `.cache_*` 같은 패턴이 있는데도 `.cache_foo/data.json`이 걸러지지 않음.

### 근본 원인
`fnmatch.fnmatch(filename, pattern)` 방식이 **파일명에만** 매칭함:
- `filename = "data.json"`
- `pattern = ".cache_*"`
- 결과: False (파일명이 `.cache_`로 시작 안 함)

`.cache_foo` 는 **폴더명**이지 파일명이 아님 → 매칭 실패 → 통과 → 하위 파일 다운로드.

### 해결책

경로 전체를 `/` 로 분할한 **모든 구성요소**를 순회하며 매칭:

```python
def should_ignore(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")
    filename = parts[-1] if parts else ""

    for pattern in IGNORE_PATTERNS:
        if pattern.endswith("/"):
            # 폴더 패턴: 구성요소에 정확히 포함되는지
            dir_name = pattern.rstrip("/")
            if dir_name in parts:
                return True
        elif "*" in pattern:
            # 글로브 패턴: 모든 구성요소에 대해 매칭 시도
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
        else:
            # 정확 매칭
            if filename == pattern or normalized == pattern:
                return True
    return False
```

**테스트 케이스** (릴리스 전 필수):
```python
assert should_ignore(".cache_histories/data.json")   # 글로브 폴더 매칭
assert should_ignore(".app_data/sub/foo.bin")        # 중첩 폴더
assert should_ignore(".cache_vector.tar.gz")         # 글로브 파일명
assert not should_ignore("docs/real_note.md")        # 정상 파일
```

---

## 4. 제외 패턴 — 카테고리별 가이드

프로젝트마다 sync 대상이 다르므로 패턴은 직접 구성해야 한다. 카테고리별 예시:

```python
IGNORE_PATTERNS: list[str] = [
    # 1) 이 동기화 프로그램 자신의 상태 파일
    ".sync/",

    # 2) 사용하는 앱의 설정/캐시 폴더 (앱 이름에 맞게)
    ".<app_name>/",              # 예: 앱이 `.app/` 같은 폴더 생성
    ".<app_name>_cache/",

    # 3) 플러그인/확장 기능이 만드는 대용량 캐시
    ".vector_cache/",            # 예: 임베딩/인덱스
    ".thumbnails/",
    "*.embedding.bin",           # 글로브로 특정 확장자

    # 4) 일시 파일
    "*.tmp",
    "*.bak",
    "*.swp",                     # vim
    "*~",                        # emacs

    # 5) OS/버전관리 메타
    ".git/",
    ".svn/",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
]
```

**패턴 설계 원칙:**
| 유형 | 문법 | 용도 |
|---|---|---|
| 폴더 이름 (정확) | `.git/` | 경로 중간에 정확히 이 이름의 폴더가 있으면 그 하위 전부 제외 |
| 글로브 (경로 구성요소) | `.cache_*` | 폴더든 파일이든, 경로 중 구성요소가 글로브와 매칭되면 제외 |
| 파일명 정확 매칭 | `.DS_Store` | 파일명이나 단일 파일 경로 정확히 일치하면 제외 |
| 글로브 (파일명) | `*.tmp` | 확장자 기반 제외 |

**조언**: 구현 후 데몬 로그에 `Drive 다운로드: ... .<앱>/...` 같은 반복 패턴이 보이면, 그 접두 폴더를 IGNORE_PATTERNS에 추가하고 양쪽 지점에서 필터링 확인.

---

## 5. Windows에서 PID 락 파일 구현

### 증상
```
OSError: [WinError 87] 매개 변수가 틀립니다
SystemError: <class 'OSError'> returned a result with an exception set
```

### 근본 원인
`os.kill(pid, 0)` 은 Unix 관용구지만 Windows에서는 **signal 0 = 유효하지 않은 파라미터** 로 간주되어 `TerminateProcess` 호출이 실패함. 게다가 Python이 이를 `SystemError`로 한 번 더 래핑해서 기존 `except OSError` 가 못 잡음.

### 해결책

```python
import os
import sys
import ctypes

def _pid_alive(pid: int) -> bool:
    """해당 PID가 살아있는지 확인 (Windows/Unix 양쪽 호환)."""
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        still = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(still) and exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True
```

동일한 로직으로 signal handler도 **Windows fallback**이 필요하다 (`loop.add_signal_handler` 는 ProactorEventLoop에서 NotImplementedError).

```python
try:
    loop.add_signal_handler(sig, _trigger)
except (NotImplementedError, RuntimeError, ValueError):
    signal.signal(sig, _os_signal_handler)  # Windows fallback
```

---

## 6. 페이지 토큰(page_token) 타이밍

Drive Changes API는 **"마지막으로 본 이후의 변경"** 만 준다. 이 책갈피가 page_token.

### 자주 하는 실수
- **reconcile 시작 시점에만** `get_initial_token()` 호출 → reconcile 중 생긴 우리 업로드가 change로 쌓임 → poller 시작하자마자 "Drive에 변경 많음" → 다운로드 폭주

### 해결 방향 (둘 중 하나)

**A. 짧은 초기 reconcile**
- 액션 수가 적으면 (예: 100 이내) 짧은 시간 내 완료 → echo suppression으로 커버 가능

**B. reconcile 후 토큰 재발급**
```python
# 초기 reconcile 완료 직후
post_reconcile_token = drive.get_initial_token()
state.page_token = post_reconcile_token
# 이후 poller는 이 토큰 이후의 변경만 봄 → 우리 업로드 스킵됨
```

**주의**: 재발급 전에 **state.save(immediate=True)** 로 flush.

---

## 7. Google Drive for Desktop과의 혼동

### 증상
사용자가 `G:\내 드라이브\<루트>\...` 같은 경로를 보고 "클라우드엔 있는데 로컬엔 없다"고 말함.

### 설명
- `G:\` 는 Google Drive **데스크톱 앱**이 마운트한 **가상 드라이브** (Drive의 뷰)
- `C:\<sync_root>\` 는 실제 로컬 폴더 (데몬이 watch하는 대상)
- 둘은 서로 다른 위치. `G:\` 에 뭔가 보이는 건 **Drive에 있다는 뜻**.

### 운영 지침
- 데몬은 `C:\` 등의 실제 로컬 폴더만 watch한다
- 사용자는 파일 편집 시 **실제 로컬 경로에서만** 작업 (가상 드라이브에서 편집 금지)
- 이유: 가상 드라이브는 파일 스트리밍이라 watchdog 이벤트가 제대로 안 올 수 있고, 양쪽 편집 시 충돌 위험

---

## 8. 초기 동기화 시간 현실 인식

| sync root 규모 | Drive 목록 | Reconcile 액션 수 | 소요 시간 |
|---|---|---|---|
| ~1,000 파일 | ~30초 | ~50개 | 2~5분 |
| ~5,000 파일 | ~3~4분 | ~300~500개 | 15~30분 |
| ~10,000 파일 | ~5~8분 | ~500~1000개 | 30분~1시간 |

**액션당 평균 시간:** 업로드·다운로드 각각 1~3초 (파일 크기·네트워크 따라).

### 사용자에게 안내할 것
- "Cold start는 길다. 중단하지 마세요."
- "state 파일이 생긴 뒤부터는 재시작이 훨씬 빠름."
- 진행률 로깅 추가 고려: `reconciler: 100/500 action 처리 중 (20%)`

---

## 9. 종료 시퀀스 엄수

비정상 종료 시 state 파일이 flush 안 돼서 drive_id 매핑이 유실되면 다음 실행이 **처음부터 다시 비교**한다 (에코 폭발의 원인).

```python
async def shutdown(watcher, poller, poll_task, engine, state):
    # 1. watcher 정지 (디바운스 타이머 취소)
    watcher.stop()

    # 2. poller 정지 (진행 중 get_changes 완료 대기)
    poller.stop()
    await asyncio.wait_for(poll_task, timeout=10.0)

    # 3. engine의 pending 큐 소진 대기
    await wait_engine_idle(engine, timeout=30.0)

    # 4. state 즉시 flush (디바운스 무시)
    state.save(immediate=True)
```

**시그널 핸들러**에서 asyncio.Event 셋팅 → run_app이 이를 await으로 대기 → set 되면 위 shutdown 실행.

---

## 10. 디버깅 체크리스트

동기화가 이상할 때 확인할 순서:

1. **에코 루프 징후**: 같은 파일이 `download: X` → `upload: X` 로 반복되나?
   - YES → 에코 억제 로직 추가 여부 확인

2. **쓰레기 파일 다운로드**: 제외 패턴에 포함되어야 할 `.cache`/`.<app>` 같은 것들이 내려오나?
   - YES → IGNORE_PATTERNS 적용 지점(3개) 모두 확인 + 글로브 패턴이 경로 구성요소 매칭하는지 확인

3. **시작 시 오래 걸림**: Drive 목록 단계가 4분 이상?
   - 파일 수 10K 근처면 정상. 중단 금지.

4. **state 파일 0 bytes**: 초기 reconcile 중 프로세스 강제 종료된 흔적
   - 깨끗한 재시작: state 파일 삭제 후 재기동

5. **Windows에서 기동 실패**: `WinError 87` 또는 `SystemError`
   - PID 락 체크가 `os.kill(pid, 0)` 쓰고 있음 → ctypes 기반으로 교체

---

## 11. 검증 기준

구현 후 다음 시나리오로 end-to-end 테스트:

```
시나리오 1: cold start
  - state 파일 없음 → 전체 비교 → 모든 액션 완료 → watcher/poller 기동

시나리오 2: warm start
  - state 파일 있음 → 증분 비교 → 소수 액션 → 기동 완료

시나리오 3: 로컬 편집 → Drive 반영
  - 파일 수정 후 5~10초 내 Drive에 업로드 확인
  - Drive mtime 갱신됐지만 에코로 재다운로드 없음

시나리오 4: Drive 편집 → 로컬 반영
  - Drive 웹에서 파일 수정 → 1분 내 로컬 반영
  - 로컬 mtime 갱신됐지만 에코로 재업로드 없음

시나리오 5: 에코 억제 확인
  - 수백 파일 다운로드 후 로그에 "echo 억제" DEBUG 메시지 다수 기록 확인
  - 같은 파일의 download→upload 반복 없음

시나리오 6: 제외 패턴
  - 로컬에 `.<ignored>/foo.bin` 생성 → Drive에 안 올라감
  - Drive에 `.<ignored>/bar.bin` 업로드 → 로컬에 안 내려옴
```

---

## 12. 다중 sync root 운영 (같은 기기에서 여러 폴더 동시 동기화)

사용자가 업무·개인·연구 등 여러 로컬 폴더를 각각 다른 Drive 폴더로 동기화하려 하는 경우가 흔하다. 설계 초기부터 **단일 인스턴스 강제** 대신 **복수 인스턴스 허용** 구조로 가는 게 낫다.

### 두 가지 운영 모델

**모델 A — 프로젝트 폴더 복제 (간단, 2~3개)**
```
C:\sync_work\      ← config.yaml + .venv + token.json (업무용 루트)
C:\sync_personal\  ← config.yaml + .venv + token.json (개인용 루트)
```
- 장점: 완전 독립, 설정 충돌 가능성 0
- 단점: `.venv/` 중복 (각 200MB)

**모델 B — 한 폴더 + CLI 인자 (4개 이상 권장)**
```
C:\sync\
├── .venv\                   (공유)
├── src\                     (공유)
├── credentials.json         (공유)
├── config-work.yaml
├── config-personal.yaml
├── config-study.yaml
├── token-work.json
├── token-personal.json
└── token-study.json
```
- 실행: `python main.py --config config-work.yaml`
- 장점: 의존성/코드 1벌로 N개 루트 운영
- 단점: 설정 누락 시 crossover 사고 가능

### 모델 B 구현에 필요한 코드 변경

`main.py`의 엔트리 함수에 `argparse`를 넣고 config 경로를 전달:

```python
def run(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="sync-daemon",
        description="Google Drive ↔ 로컬 양방향 동기화 데몬",
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="설정 파일 경로 (기본: config.yaml)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(main(args.config))
    except KeyboardInterrupt:
        return 0
```

테스트에서는 `run([])` 으로 pytest 인자가 파싱되지 않도록 방어.

### 다중 운영 시 필수 원칙 5가지

| 원칙 | 어기면 |
|---|---|
| 각 config의 **`folder_id` (remote root) 는 서로 달라야** | 같은 Drive 폴더를 두 데몬이 가리키면 크로스 동기화 → 파일 삭제/충돌 폭발 |
| 각 config의 **로컬 sync root 는 서로 달라야** | 같은 로컬 폴더를 두 데몬이 감시 → 에코 루프 |
| 각 config의 **`token_file` 은 서로 달라야** | 같은 token 파일 공유 → 서로의 OAuth 세션 덮어쓰기 |
| 각 config의 **`log_file` 도 분리 권장** | 로그 뒤섞여서 디버깅 불가 |
| OS 서비스 이름 서로 달라야 함 (NSSM/systemd/launchd) | 등록 실패 또는 덮어쓰기 |

### 각 config에 다른 device_id 지정 권장

충돌 사본 생성 시 `{원본}.conflict-{device_id}-{시각}.{ext}` 형식을 흔히 쓰므로, **sync root 구분용 device_id** 를 권장:

```yaml
# config-work.yaml
device_id: my_pc_work

# config-personal.yaml
device_id: my_pc_personal
```

없으면 `socket.gethostname()` 이 기본값으로 쓰여서 같은 PC의 두 루트가 동일 device_id를 갖게 되어 충돌 사본이 덮일 수 있다.

### NSSM 등록 예시 (Windows, 모델 B)

| 서비스명 | Path | Arguments | Startup directory |
|---|---|---|---|
| SyncWork | `C:\sync\.venv\Scripts\python.exe` | `main.py --config config-work.yaml` | `C:\sync` |
| SyncPersonal | `C:\sync\.venv\Scripts\python.exe` | `main.py --config config-personal.yaml` | `C:\sync` |

같은 Python 바이너리를 공유, **Arguments만 다르게** 주면 각각 독립 프로세스로 실행.

### systemd (Linux) 템플릿 서비스로 한꺼번에

systemd는 template unit (`name@.service`) 으로 여러 인스턴스 관리 가능:

`/etc/systemd/system/gdrive-sync@.service`:
```ini
[Unit]
Description=Google Drive Sync (%i)
After=network-online.target

[Service]
Type=simple
User=USER
WorkingDirectory=/opt/sync
ExecStart=/opt/sync/.venv/bin/python main.py --config config-%i.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now gdrive-sync@work
systemctl enable --now gdrive-sync@personal
```

`%i` 가 인스턴스명으로 치환됨. 확장성 최고.

---

## 13. 요약: "반드시 피할 12가지"

### 구현 단계
1. ❌ 에코 억제 없이 양방향 sync 짜지 말 것
2. ❌ IGNORE_PATTERNS를 로컬 scan에만 적용하지 말 것 (Drive 쪽도 필터)
3. ❌ 글로브 패턴을 파일명에만 매칭하지 말 것 (경로 구성요소 전체)
4. ❌ `os.kill(pid, 0)` 으로 Windows PID 체크하지 말 것 (ctypes OpenProcess)
5. ❌ page_token을 reconcile 시작 시점으로만 고정하지 말 것
6. ❌ 종료 시 state.save(immediate=True) 호출 빠뜨리지 말 것
7. ❌ Drive Desktop(가상 드라이브)과 실제 로컬 폴더 혼용하지 말 것
8. ❌ 단일 인스턴스만 전제하고 config를 하드코딩하지 말 것 (--config 인자 지원)

### 다중 sync root 운영
9. ❌ 두 config가 같은 `folder_id` 를 가리키게 하지 말 것
10. ❌ 두 config가 같은 로컬 sync root를 감시하게 하지 말 것
11. ❌ 두 config가 같은 `token_file` 을 공유하게 하지 말 것
12. ❌ 같은 PC의 여러 루트에 같은 `device_id` 를 쓰지 말 것 (충돌 사본 덮어씀)

이 12가지만 지켜도 시행착오 **수 시간 → 10분**으로 줄어든다.
