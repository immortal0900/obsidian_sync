# Obsidian Vault Bidirectional Sync — 전체 기획서

> Claude Code 구현용 프로젝트 명세서
> 작성일: 2026-04-14

---

## 1. 프로젝트 개요

### 한 줄 요약
로컬 Obsidian 볼트와 Google Drive 사이의 양방향 자동 동기화 프로그램.
파일이 어느 쪽에서 바뀌든 자동으로 반대편에 반영되고, 삭제도 정확히 전파된다.

### 왜 만드나?
- 기존 커뮤니티 플러그인(Remotely Save, LiveSync 등)은 각각 한계가 있음
  - Remotely Save: 분 단위 폴링, 플러그인 밖에서 삭제하면 파일이 부활
  - LiveSync: 실시간이지만 CouchDB 서버 필요, hidden file 충돌 빈발, 설정 복잡
- Obsidian 플러그인이 아닌 **독립 프로그램**으로 만들어서 Obsidian이 꺼져 있어도 동기화

### 기술 스택
- Python 3.12
- watchdog (로컬 파일 감시)
- Google Drive API v3 (클라우드 통신)
- JSON 파일 (상태 저장 — DB 안 씀)
- 3단계 구조: watchdog → hook registry → LangGraph (향후 확장)

---

## 2. 핵심 설계 원칙

### 2-1. 이벤트 기반 (폴링 아님)

로컬 변경 감지는 주기적으로 파일 목록을 훑는 게 아니라,
운영체제가 "파일이 바뀌었어!"라고 알려주는 이벤트를 받아서 처리한다.
- Linux: inotify
- macOS: FSEvents  
- Windows: ReadDirectoryChangesW

watchdog 라이브러리가 이 세 가지를 통합해서 제공함.

### 2-2. 클라우드 변경 감지는 증분 방식

Google Drive에 "전체 파일 목록 줘"라고 매번 요청하는 게 아니라,
"내가 마지막으로 확인한 이후로 바뀐 것만 줘"라고 요청한다.
- Google Drive Changes API 사용
- "여기까지 확인했어요"라는 책갈피(page token)를 저장해두고 재사용
- 이 책갈피는 만료되지 않음 → 프로그램이 일주일 꺼져 있어도 재개 가능

### 2-3. 삭제는 명시적으로 기록

양방향 동기화에서 가장 어려운 문제:
"클라우드에 이 파일이 없다" → 새 파일이라 아직 안 올라간 건가, 누가 삭제한 건가?

해결책:
- 로컬 삭제: watchdog이 삭제 이벤트를 직접 감지 → 즉시 Drive에서도 삭제
- 클라우드 삭제: Changes API 응답에 `removed: true` 필드가 포함됨 → 로컬에서도 삭제
- 프로그램 꺼진 동안의 삭제: 상태 파일(JSON)에 "마지막으로 알고 있던 파일 목록"을 저장해두고, 재시작 시 현재 파일과 비교

### 2-4. 상태 저장은 JSON 파일 한 장

DB를 쓰지 않는다. 필요한 정보는:
- 책갈피 (page token)
- 마지막 동기화 시각
- 파일별 {경로, 수정시각, 크기, Drive 파일 ID}

이 JSON은 매번 현재 상태로 덮어쓰기 하므로 크기가 누적되지 않는다.
볼트에 파일 1,000개 → JSON 약 50~100KB 고정.

---

## 3. 폴더 구조

```
obsidian-vault-sync/
├── CLAUDE.md                    # Claude Code 작업 지침서 (아래 별도 섹션)
├── pyproject.toml               # 프로젝트 설정 + 의존성
├── src/
│   ├── __init__.py
│   ├── main.py                  # 진입점: 프로그램 시작/종료 관리
│   ├── config.py                # 설정 로드 (볼트 경로, Drive 폴더 ID 등)
│   ├── state.py                 # 상태 파일(JSON) 읽기/쓰기/비교
│   ├── local_watcher.py         # watchdog 기반 로컬 파일 감시
│   ├── drive_client.py          # Google Drive API 래퍼 (업로드/다운로드/삭제/변경목록)
│   ├── poller.py                # 적응형 폴링 (클라우드 → 로컬 변경 감지)
│   ├── reconciler.py            # 재시작 시 양쪽 상태 비교 + 충돌 판정
│   ├── conflict.py              # 충돌 처리 전략 (충돌 사본 생성)
│   └── sync_engine.py           # 동기화 실행: 업로드/다운로드/삭제 조율
├── tests/
│   ├── test_state.py
│   ├── test_reconciler.py
│   ├── test_conflict.py
│   └── test_sync_engine.py
├── credentials/                  # .gitignore에 포함
│   └── service_account.json
└── docs/
    └── setup_guide.md            # Google Cloud 프로젝트 설정 가이드
```

---

## 4. 상태 파일 설계

### 4-1. 파일 위치
```
볼트폴더/.sync/sync_state.json
```
`.sync/` 폴더는 동기화 대상에서 제외한다.

### 4-2. 구조

```json
{
  "version": 1,
  "device_id": "my_pc",
  "page_token": "67890",
  "last_synced_at": 1713100000.0,
  "files": {
    "daily/2026-04-14.md": {
      "mtime": 1713099000.0,
      "size": 2048,
      "drive_id": "1aBcDeFgHiJkLmNoPqRsT"
    },
    "projects/ALL_FOR_ONE.md": {
      "mtime": 1713098000.0,
      "size": 15360,
      "drive_id": "2uVwXyZ0123456789AbC"
    }
  }
}
```

### 4-3. 갱신 규칙
- **정상 운영 중**: 파일이 동기화될 때마다 해당 항목 갱신, 5초마다 디스크에 쓰기 (디바운스)
- **프로그램 종료 시**: 즉시 디스크에 쓰기
- **갱신 방식**: 항상 전체 덮어쓰기 (append 아님)

---

## 5. 모듈별 상세 설계

### 5-1. config.py — 설정

```python
# 필요한 설정 항목
VAULT_PATH = "볼트 폴더 절대경로"
DRIVE_FOLDER_ID = "Google Drive에서 볼트를 저장할 폴더의 ID"
DEVICE_ID = "이 기기의 고유 이름 (예: my_pc, galaxy_s25)"
SYNC_STATE_DIR = ".sync"

# 동기화 제외 패턴
IGNORE_PATTERNS = [
    ".obsidian/workspace.json",       # Obsidian이 열 때마다 바뀜
    ".obsidian/workspace-mobile.json",
    ".obsidian/cache/",
    ".sync/",                          # 상태 파일 자체
    ".trash/",
    ".DS_Store",
    "*.tmp",
]

# 적응형 폴링 설정
POLL_MIN_INTERVAL = 10     # 활발할 때 최소 10초
POLL_MAX_INTERVAL = 120    # 조용할 때 최대 2분
POLL_START_INTERVAL = 30   # 시작 간격 30초
POLL_BACKOFF_FACTOR = 1.5  # 변경 없으면 간격을 1.5배씩 늘림
```

### 5-2. state.py — 상태 파일 관리

**역할**: sync_state.json 읽기/쓰기, 현재 파일 시스템 스캔, 차이점 계산

```python
class SyncState:
    """상태 파일 관리"""
    
    def load(self) -> dict:
        """sync_state.json을 읽어서 반환. 없으면 None."""
    
    def save(self, state: dict):
        """현재 상태를 sync_state.json에 덮어쓰기."""
    
    def scan_local_files(self) -> dict:
        """볼트 폴더를 스캔해서 {경로: {mtime, size}} 반환.
        IGNORE_PATTERNS에 해당하는 파일은 건너뜀."""
    
    def diff(self, old_files: dict, new_files: dict) -> dict:
        """두 파일 목록을 비교해서 추가/수정/삭제 분류.
        
        반환값:
        {
            "added": ["새파일1.md", "새파일2.md"],
            "modified": ["수정된파일.md"],
            "deleted": ["삭제된파일.md"]
        }
        """
```

### 5-3. local_watcher.py — 로컬 파일 감시

**역할**: watchdog으로 파일 변경 감지, 디바운스 후 동기화 요청

```python
class LocalWatcher:
    """watchdog 기반 파일 감시"""
    
    def __init__(self, vault_path, sync_engine, debounce_seconds=2.0):
        """
        vault_path: 볼트 폴더 경로
        sync_engine: 변경 발생 시 호출할 동기화 엔진
        debounce_seconds: 파일 변경 후 이 시간만큼 기다렸다가 동기화 (기본 2초)
            → 빠르게 연속 저장할 때 매번 동기화하지 않도록 방지
        """
    
    def start(self):
        """감시 시작."""
    
    def stop(self):
        """감시 중단."""
    
    # watchdog 이벤트 핸들러
    def on_created(self, event):
        """파일 생성됨 → 디바운스 후 업로드 요청"""
    
    def on_modified(self, event):
        """파일 수정됨 → 디바운스 후 업로드 요청"""
    
    def on_deleted(self, event):
        """파일 삭제됨 → Drive에서도 삭제 요청"""
    
    def on_moved(self, event):
        """파일 이동/이름변경 → Drive에서 이름 변경 요청"""
    
    def _should_ignore(self, path) -> bool:
        """IGNORE_PATTERNS에 해당하면 True"""
    
    def last_event_age(self) -> float:
        """마지막 이벤트로부터 경과 시간(초). 폴링 간격 조절에 사용."""
```

**디바운스 동작 방식**:
```
사용자가 파일을 빠르게 3번 저장:
  t=0.0s  on_modified → 타이머 시작 (2초 후 동기화 예약)
  t=0.5s  on_modified → 타이머 리셋 (다시 2초 후로)
  t=1.0s  on_modified → 타이머 리셋 (다시 2초 후로)
  t=3.0s  타이머 만료 → 동기화 1회 실행 (3번 저장에 대해 1번만 동기화)
```

### 5-4. drive_client.py — Google Drive API 통신

**역할**: Drive API 호출을 감싸서 단순한 함수로 제공

```python
class DriveClient:
    """Google Drive API 래퍼"""
    
    def __init__(self, credentials_path, folder_id):
        """
        credentials_path: 서비스 계정 또는 OAuth 인증 파일 경로
        folder_id: Drive에서 볼트를 저장할 폴더의 ID
        """
    
    # === 파일 조작 ===
    
    def upload(self, local_path, relative_path) -> str:
        """로컬 파일을 Drive에 업로드.
        이미 있으면 덮어쓰기(update), 없으면 새로 생성(create).
        반환값: Drive 파일 ID"""
    
    def download(self, drive_file_id, local_path):
        """Drive 파일을 로컬에 다운로드."""
    
    def delete(self, drive_file_id):
        """Drive 파일 삭제 (휴지통으로 이동)."""
    
    def rename(self, drive_file_id, new_name):
        """Drive 파일 이름 변경."""
    
    # === 변경 감지 ===
    
    def get_initial_token(self) -> str:
        """처음 사용할 때 책갈피(page token)를 발급받음."""
    
    def get_changes(self, page_token) -> tuple[list, str]:
        """책갈피 이후의 변경 목록을 가져옴.
        
        반환값: (변경 목록, 새 책갈피)
        
        변경 목록의 각 항목:
        {
            "file_id": "...",
            "removed": True/False,   # True면 삭제됨
            "file": {                # removed가 False일 때만 존재
                "name": "파일명",
                "modified_time": "...",
                "md5": "..."
            }
        }
        """
    
    # === 전체 목록 (상태 파일 없을 때만 사용) ===
    
    def list_all_files(self) -> list:
        """Drive 폴더의 전체 파일 목록.
        첫 실행이나 상태 파일 분실 시에만 사용."""
```

### 5-5. poller.py — 적응형 폴링

**역할**: 클라우드 변경을 주기적으로 확인. 변경이 많으면 자주, 조용하면 드물게.

```python
class AdaptivePoller:
    """클라우드 변경 감지용 적응형 폴링"""
    
    def __init__(self, drive_client, sync_engine, local_watcher):
        """
        drive_client: Drive API 통신 담당
        sync_engine: 변경 발견 시 호출할 동기화 엔진
        local_watcher: 로컬 활동 여부 확인용
        """
        self.current_interval = POLL_START_INTERVAL  # 시작: 30초
    
    async def run(self):
        """폴링 루프 시작"""
    
    def _calc_next_interval(self, had_changes: bool) -> float:
        """다음 폴링까지 대기 시간 계산.
        
        규칙:
        1. 클라우드에서 변경이 있었음 → 10초 (빨리 다시 확인)
        2. 클라우드 변경은 없지만 로컬에서 편집 중 → 30초
        3. 양쪽 다 조용 → 점진적으로 늘림 (최대 2분)
        """
```

**간격 변화 예시**:
```
다른 기기에서 편집 중:
  10초 → 10초 → 10초 → (편집 멈춤) → 15초 → 22초 → 33초 → 50초 → 75초 → 112초 → 120초(최대)
```

### 5-6. reconciler.py — 재시작 시 상태 맞추기

**역할**: 프로그램 재시작 시 로컬과 클라우드의 차이를 파악하고 합침

```python
class Reconciler:
    """재시작 시 양쪽 상태 비교 + 병합"""
    
    def run(self) -> list:
        """
        재시작 시 실행되는 전체 흐름:
        
        1. 상태 파일(JSON) 로드
        2. 로컬 변경 감지: JSON의 파일 목록 vs 현재 실제 파일
        3. 클라우드 변경 감지: 저장된 책갈피로 Changes API 호출
        4. 양쪽 결과를 파일별로 대조
        5. 충돌이 없는 항목은 바로 동기화
        6. 충돌이 있는 항목은 conflict.py에 위임
        
        반환값: 동기화 작업 목록
        """
    
    def run_without_state(self) -> list:
        """
        상태 파일이 없을 때 (첫 실행 또는 파일 분실):
        
        1. Drive 전체 파일 목록을 가져옴
        2. 로컬 전체 파일 목록을 스캔
        3. 양쪽을 파일명으로 매칭
        4. 한쪽에만 있는 파일 → 그쪽에서 반대쪽으로 복사
        5. 양쪽 다 있는 파일 → 수정시각 비교해서 최신 것 채택
        6. 새 책갈피 발급받아서 상태 파일 생성
        """
```

**대조 규칙표**:
```
                    │ 클라우드:     │ 클라우드:    │ 클라우드:     │ 클라우드:
                    │ 변경 없음     │ 새 파일      │ 수정됨       │ 삭제됨
────────────────────┼──────────────┼─────────────┼─────────────┼─────────────
로컬: 변경 없음      │ 아무것도 안함  │ 다운로드     │ 다운로드      │ (이미 없음)
로컬: 새 파일        │ 업로드       │ ★ 충돌       │ 해당없음      │ 해당없음
로컬: 수정됨         │ 업로드       │ 해당없음     │ ★ 충돌        │ ★ 충돌
로컬: 삭제됨         │ Drive도 삭제  │ 해당없음     │ ★ 충돌        │ 양쪽 삭제 OK
```

### 5-7. conflict.py — 충돌 처리

**역할**: 양쪽에서 동시에 변경된 파일 처리

```python
class ConflictResolver:
    """충돌 발생 시 처리"""
    
    def resolve(self, path, local_info, remote_info) -> str:
        """
        전략: 충돌 사본 생성 (양쪽 모두 보존)
        
        예시:
          원본: daily/2026-04-14.md
          충돌 사본: daily/2026-04-14.conflict-my_pc-20260414-153000.md
          
          → 원본은 클라우드 버전으로 덮어쓰기
          → 충돌 사본에 로컬 버전 보존
          → 사용자가 직접 확인 후 정리
        
        반환값: "conflict_created" 또는 "auto_resolved"
        """
```

**충돌 사본 이름 규칙**:
```
{원본이름}.conflict-{기기이름}-{날짜시각}.md

예시:
  note.conflict-my_pc-20260414-153000.md
  note.conflict-galaxy_s25-20260414-160000.md
```

### 5-8. sync_engine.py — 동기화 실행

**역할**: 실제 파일 업로드/다운로드/삭제를 실행하고 상태 파일을 갱신

```python
class SyncEngine:
    """동기화 작업 실행"""
    
    def __init__(self, drive_client, state, conflict_resolver):
        self.lock = False  # 동시 실행 방지용 잠금
    
    def execute(self, action):
        """
        하나의 동기화 작업을 실행.
        
        action 종류:
        - {"type": "upload", "path": "...", "reason": "로컬에서 생성됨"}
        - {"type": "download", "file_id": "...", "path": "...", "reason": "클라우드에서 수정됨"}
        - {"type": "delete_remote", "file_id": "...", "reason": "로컬에서 삭제됨"}
        - {"type": "delete_local", "path": "...", "reason": "클라우드에서 삭제됨"}
        - {"type": "conflict", "path": "...", "local": {...}, "remote": {...}}
        
        실행 후 상태 파일의 해당 항목을 갱신.
        """
    
    def handle_local_change(self, event_type, path):
        """watchdog 이벤트 처리 (로컬 → 클라우드)"""
    
    def handle_remote_changes(self, changes):
        """폴링 결과 처리 (클라우드 → 로컬)"""
    
    def _acquire_lock(self) -> bool:
        """동시 실행 방지. 이미 동기화 중이면 False."""
    
    def _release_lock(self):
        """잠금 해제."""
```

### 5-9. main.py — 진입점

```python
"""프로그램 시작/종료 관리"""

async def main():
    # 1. 설정 로드
    config = load_config()
    
    # 2. Google Drive 인증
    drive = DriveClient(config.credentials_path, config.drive_folder_id)
    
    # 3. 상태 파일 확인 + 재시작 동기화
    state = SyncState(config.vault_path)
    reconciler = Reconciler(state, drive)
    
    saved_state = state.load()
    if saved_state:
        # 정상 경로: 상태 파일 있음 → 증분 비교
        actions = reconciler.run()
    else:
        # 첫 실행 또는 상태 파일 분실 → 전체 비교
        actions = reconciler.run_without_state()
    
    # 4. 밀린 동기화 실행
    engine = SyncEngine(drive, state, ConflictResolver(config.device_id))
    for action in actions:
        engine.execute(action)
    
    # 5. 감시 시작
    watcher = LocalWatcher(config.vault_path, engine)
    watcher.start()
    
    poller = AdaptivePoller(drive, engine, watcher)
    asyncio.create_task(poller.run())
    
    # 6. 종료 신호 대기 (Ctrl+C)
    try:
        await shutdown_event.wait()
    finally:
        watcher.stop()
        state.save(current_state)  # 최종 상태 저장
```

---

## 6. 동작 흐름 정리

### 6-1. 프로그램 시작 시

```
프로그램 시작
    │
    ├─ sync_state.json 있나?
    │     │
    │     ├─ 있음 ──────────────────────────────────────┐
    │     │                                             │
    │     │   [로컬 비교]                                │
    │     │   JSON의 파일목록 vs 지금 실제 파일           │
    │     │   → 새로 생긴 파일, 수정된 파일, 삭제된 파일   │
    │     │                                             │
    │     │   [클라우드 비교]                             │
    │     │   저장된 책갈피로 Changes API 호출             │
    │     │   → 클라우드에서 바뀐 파일 목록                │
    │     │                                             │
    │     │   [합치기]                                   │
    │     │   한쪽만 바뀜 → 바로 반영                     │
    │     │   양쪽 다 바뀜 → 충돌 사본 생성               │
    │     │                                             │
    │     └─ 없음 ──────────────────────────────────────┐
    │                                                   │
    │         Drive 전체 파일 목록 받아오기               │
    │         로컬 전체 파일 목록 스캔                    │
    │         양쪽 비교 후 동기화                         │
    │         새 책갈피 발급 + 상태 파일 생성              │
    │                                                   │
    ├───────────────────────────────────────────────────┘
    │
    ▼
  상태 파일 갱신 + watchdog 시작 + 폴링 시작
```

### 6-2. 정상 운영 중 — 로컬에서 파일 수정

```
사용자가 Obsidian에서 노트 저장
    │
    ▼
watchdog이 on_modified 이벤트 수신
    │
    ▼
제외 대상인지 확인 (workspace.json 등)
    │
    ├─ 제외 대상 → 무시
    │
    └─ 동기화 대상 → 디바운스 타이머 시작/리셋 (2초)
                        │
                        ▼ (2초 경과, 추가 변경 없음)
                    Drive에 업로드 (update 또는 create)
                        │
                        ▼
                    상태 파일에서 해당 항목 갱신
```

### 6-3. 정상 운영 중 — 클라우드에서 파일 변경

```
적응형 폴링 타이머 도달
    │
    ▼
Changes API 호출 (저장된 책갈피 사용)
    │
    ├─ 변경 없음 → 폴링 간격 늘림 (×1.5, 최대 2분) → 대기
    │
    └─ 변경 있음 → 폴링 간격 10초로 줄임
                    │
                    ▼
                변경 목록 순회:
                    │
                    ├─ removed: true → 로컬 파일 삭제
                    │
                    └─ removed: false → 로컬에 다운로드
                                        (이미 있으면 수정시각 비교)
                    │
                    ▼
                상태 파일 갱신 + 새 책갈피 저장
```

### 6-4. 프로그램 종료 시

```
Ctrl+C 또는 시스템 종료 신호
    │
    ▼
watchdog 중단
    │
    ▼
현재 진행 중인 동기화 완료 대기
    │
    ▼
상태 파일에 최종 상태 저장 (책갈피 + 파일 목록)
    │
    ▼
프로그램 종료
```

---

## 7. 제외 대상 상세

### 동기화하면 안 되는 파일들

| 경로/패턴 | 이유 |
|-----------|------|
| `.obsidian/workspace.json` | Obsidian이 열 때마다 자동 변경됨 → 충돌 폭탄 |
| `.obsidian/workspace-mobile.json` | 위와 동일 (모바일 버전) |
| `.obsidian/cache/` | 캐시 폴더, 동기화 불필요 |
| `.sync/` | 이 프로그램의 상태 파일 |
| `.trash/` | Obsidian 휴지통 |
| `.DS_Store` | macOS 시스템 파일 |
| `*.tmp` | 임시 파일 |

### 동기화해도 되지만 주의가 필요한 파일들

| 경로/패턴 | 주의사항 |
|-----------|----------|
| `.obsidian/plugins/*/data.json` | 플러그인 설정, 기기별로 다를 수 있음 |
| `.obsidian/appearance.json` | 테마 설정, 기기별로 다를 수 있음 |
| `.obsidian/hotkeys.json` | 단축키, 기기별로 다를 수 있음 |

→ 1차 구현에서는 `.obsidian/` 폴더 전체를 제외하고, 이후 옵션으로 추가

---

## 8. 충돌 처리 상세

### 충돌 발생 조건
프로그램이 꺼져 있는 동안, **같은 파일을** 로컬과 클라우드 양쪽에서 수정한 경우.

### 처리 방식: 충돌 사본 생성

```
원본 파일: daily/2026-04-14.md

로컬 버전 (내가 PC에서 수정한 것):
  → daily/2026-04-14.conflict-my_pc-20260414-153000.md 로 사본 생성

클라우드 버전 (다른 기기에서 수정한 것):
  → daily/2026-04-14.md 에 반영 (원본 자리에)

결과: 파일 2개가 남고, 사용자가 직접 비교 후 정리
```

### 왜 이 방식인가
- "최신 파일 우선" 방식은 이전 내용이 사라질 수 있음
- "자동 병합" 방식은 마크다운 특성상 깨질 수 있음
- "양쪽 다 보존"이 노트 앱에서는 가장 안전함
- 파일 하나가 더 생기는 건 불편하지만, 내용이 날아가는 것보다 나음

---

## 9. 오류 처리

### 9-1. 네트워크 끊김
- Drive API 호출 실패 → 재시도 (최대 3회, 간격 점점 늘림)
- 3회 모두 실패 → 해당 작업을 "대기열"에 넣고 다음 폴링 때 재시도
- 오프라인 지속 → watchdog은 계속 동작, 변경은 상태 파일에 기록, 온라인 복귀 시 일괄 동기화

### 9-2. 파일 잠금 (다른 프로그램이 사용 중)
- 다운로드 시 쓰기 실패 → 1초 후 재시도 (최대 3회)
- 업로드 시 읽기 실패 → 1초 후 재시도 (최대 3회)

### 9-3. 상태 파일 손상
- JSON 파싱 실패 → 상태 파일 없는 것으로 간주 → 전체 비교 모드로 전환
- 기존 손상 파일은 `.sync/sync_state.json.backup`으로 이름 변경

### 9-4. Google Drive API 할당량 초과
- 429 응답 수신 → 지수적 대기 (1초 → 2초 → 4초 → ... 최대 5분)
- 폴링 간격을 최대값(2분)으로 고정

---

## 10. 구현 순서

### 1단계: 기반 (먼저 구현)
1. `config.py` — 설정 로드
2. `state.py` — 상태 파일 읽기/쓰기/스캔/비교
3. `drive_client.py` — Drive API 인증 + 업로드/다운로드/삭제/변경목록

### 2단계: 핵심 동기화
4. `sync_engine.py` — 동기화 작업 실행
5. `reconciler.py` — 재시작 시 상태 맞추기
6. `conflict.py` — 충돌 사본 생성

### 3단계: 실시간 감시
7. `local_watcher.py` — watchdog 설정 + 디바운스
8. `poller.py` — 적응형 폴링

### 4단계: 통합
9. `main.py` — 전체 조립 + 시작/종료 흐름
10. 테스트 작성

### 5단계: 향후 확장 (이번 범위 밖)
- hook registry 패턴 도입
- LangGraph 연동 (Stage 3)
- 다중 볼트 지원

---

## 11. CLAUDE.md (Claude Code 작업 지침)

아래 내용을 프로젝트 루트의 `CLAUDE.md`에 그대로 넣어서 사용한다.

```markdown
# CLAUDE.md

## 프로젝트 개요
Obsidian 볼트와 Google Drive 사이의 양방향 자동 동기화 프로그램.

## 기술 스택
- Python 3.12, uv (패키지 관리)
- watchdog (로컬 파일 감시)
- Google Drive API v3
- asyncio (비동기 처리)

## 코드 규칙
- 타입 힌트 필수
- 함수/클래스에 한국어 docstring 작성
- 변수명은 영어, 주석은 한국어
- f-string 사용 (format() 금지)
- 로깅은 logging 모듈 사용 (print 금지)

## 파일별 역할
- config.py: 설정값 정의 및 로드
- state.py: sync_state.json 관리 (읽기/쓰기/비교)
- drive_client.py: Google Drive API 래핑
- local_watcher.py: watchdog 기반 파일 감시
- poller.py: 적응형 폴링 (클라우드 변경 감지)
- reconciler.py: 재시작 시 양쪽 상태 비교
- conflict.py: 충돌 사본 생성
- sync_engine.py: 실제 동기화 실행
- main.py: 진입점

## 핵심 설계 결정
1. DB 사용하지 않음 — JSON 파일 한 장으로 상태 관리
2. 폴링이 아니라 이벤트 기반 — watchdog + Changes API
3. 충돌 시 양쪽 보존 — .conflict 사본 생성
4. .obsidian/ 폴더는 1차 구현에서 동기화 제외

## 테스트
- pytest 사용
- tests/ 폴더에 모듈별 테스트
- Drive API 호출은 mock 처리

## 구현 순서
1단계: config.py → state.py → drive_client.py
2단계: sync_engine.py → reconciler.py → conflict.py
3단계: local_watcher.py → poller.py
4단계: main.py → 통합 테스트
```

---

## 12. 의존성 목록

```toml
# pyproject.toml
[project]
name = "obsidian-vault-sync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "watchdog>=4.0",
    "google-api-python-client>=2.100",
    "google-auth-httplib2>=0.2",
    "google-auth-oauthlib>=1.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```
