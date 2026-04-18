# 다른 기기에서 obsidian_sync 설정하기

> 새 PC, 노트북, 서버에서 이 프로젝트를 돌리기 위한 완전한 절차.
> `git clone` 하는 경우와 **폴더 통째 복사** 하는 경우 양쪽을 다룬다.

---

## 빠른 요약 (익숙한 분용)

1. 프로젝트 가져오기 (clone 또는 복사)
2. `uv sync` 로 의존성 설치
3. `credentials.json` 만 기존 기기에서 복사 (다른 인증 파일은 제외)
4. `config.yaml` 이 기기 볼트 경로로 새로 작성
5. Obsidian으로 볼트 폴더 한번 열기 (.obsidian/ 생성 트리거) — 선택
6. `uv run python main.py` 로 첫 실행 → 브라우저 인증
7. 정상 동작 확인 후 `nssm install ObsidianSync` 로 자동 실행 등록

실제 환경별 세부 사항은 아래에서 설명.

---

## 시나리오 A: `git clone` 으로 가져오기

### A-1. 레포 복제

```bash
git clone <repo-url> obsidian_sync
cd obsidian_sync
```

> **주의:** 기기별 비밀(credentials.json, token.json, config.yaml)은 `.gitignore`에 포함되어 있어 repo에 없습니다. **직접 준비해야 합니다.**

### A-2. uv 설치

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### A-3. 의존성 설치

```bash
uv sync
```

→ `.venv/` 가 생성됩니다 (기기 전용, 복사 X)

---

## 시나리오 B: **폴더 통째 복사** 해서 가져오기

USB, 공유 폴더, 클라우드 등으로 `obsidian_sync/` 전체를 복사한 경우.

### B-1. 새 기기 맞게 안 맞는 것들 제거

복사해온 폴더에서 **반드시 삭제할 대상:**

| 파일/폴더 | 이유 |
|---|---|
| `.venv/` | Python 가상환경은 OS/경로 의존적 |
| `token.json` | 기기별 OAuth 토큰 (사용 시 충돌) |
| `config.yaml` | 이전 기기의 볼트 경로가 적혀 있음 |
| `obsidian_sync.log*` | 이전 기기 로그 |
| `daemon.lock` | 이전 기기 PID 찌꺼기 |
| `.sync/` (볼트 내부) | 이전 기기의 상태 파일 (볼트는 새 기기에 맞게 재스캔) |

**cmd 에서 일괄 처리 예시:**
```cmd
cd C:\path\to\obsidian_sync
rmdir /S /Q .venv
del token.json config.yaml obsidian_sync.log* daemon.lock 2>nul
```

볼트 쪽도:
```cmd
rmdir /S /Q "C:\새기기\볼트경로\.sync"
```

### B-2. uv 설치 & 의존성 재구축

시나리오 A의 A-2, A-3 와 동일. `.venv/` 가 이 기기용으로 새로 만들어집니다.

---

## 공통 단계 (시나리오 A, B 이후)

### 1. `credentials.json` 배치

이건 **모든 기기에서 동일한 파일** 입니다. Google Cloud Console에서 발급한 OAuth 클라이언트 시크릿.

**방법 1: 기존 기기에서 복사 (권장)**

USB, 이메일, 암호화 공유 등으로 옮깁니다.

```
[기존 기기] obsidian_sync/credentials.json  →  [새 기기] obsidian_sync/credentials.json
```

**방법 2: Google Cloud Console에서 다시 다운로드**

1. https://console.cloud.google.com 접속
2. 기존 프로젝트 선택
3. API 및 서비스 → **클라이언트**
4. OAuth 2.0 클라이언트 ID 클릭 → **JSON 다운로드**
5. 파일명을 `credentials.json` 으로 변경 → 프로젝트 루트에 배치

> `credentials.json` 은 **본질상 민감 정보**입니다. 공용 클라우드, 공개 repo, 공유 드라이브에 올리지 마세요.

### 2. `config.yaml` 작성

이 파일은 **이 기기 전용**입니다. 예시:

```yaml
watch_paths:
  - path: C:/Users/YOUR_NAME/ObsidianVault   # (1) 이 기기의 볼트 절대경로
    hooks: [sync]

drive:
  credentials_file: credentials.json
  token_file: token.json
  folder_id: 1K1sGu96jsYawdJnJ9czj3CNsRhM7yOU6   # (2) 기존 기기와 같은 값

sync:
  debounce_seconds: 5
  poll_interval_seconds: 60
  delete_local: false

logging:
  level: INFO
  file: obsidian_sync.log
  max_bytes: 5242880
  backup_count: 3
```

**수정해야 할 3가지:**

| 필드 | 새 기기에서 | 주의 |
|---|---|---|
| `watch_paths[0].path` | 이 기기의 실제 볼트 폴더 | 슬래시(`/`) 사용, 따옴표 없이 |
| `drive.folder_id` | 기존 기기와 **동일 값** | Drive 같은 폴더 공유해야 동기화됨 |
| `device_id` (선택) | 이 기기만의 이름 (예: `laptop`, `home_pc`) | 충돌 사본 네이밍에 사용됨. 없으면 자동으로 hostname 사용 |

**`folder_id` 찾는 법:**
- 기존 기기 `config.yaml` 의 값 복사 (제일 쉬움)
- 또는 Drive 웹에서 폴더 열고 URL에서: `https://drive.google.com/drive/folders/1K1sGu...` 에서 뒤쪽 ID 부분

**자주 하는 실수:**

| 틀린 예 | 고친 예 |
|---|---|
| `path: C:\Users\Name\Vault` | `path: C:/Users/Name/Vault` |
| `path: "C:/Users/Name/Vault"` | `path: C:/Users/Name/Vault` (따옴표 X) |
| `folder_id: https://drive.google.com/drive/folders/1K1s...` | `folder_id: 1K1sGu96jsYawdJnJ9czj3CNsRhM7yOU6` |

### 3. 볼트 폴더 준비

**경우 1: 빈 볼트로 시작** (Drive에 이미 파일 있음)
- 빈 폴더를 `config.yaml` 의 `watch_paths[0].path` 위치에 만들어 둠
- 첫 실행 시 reconciler가 Drive의 모든 파일을 다운로드

**경우 2: 기존 볼트 있음** (이미 로컬에 메모 있는 기기)
- 볼트의 `.sync/` 폴더는 **삭제** 후 시작 (다른 기기의 상태를 가져오지 마세요)
- 첫 실행 시 reconciler가 로컬과 Drive 비교:
  - 한쪽에만 있는 파일 → 복사
  - 양쪽 다 있는데 mtime 다름 → 최신 쪽 채택

**경우 3: 볼트가 Google Drive Desktop(G:\) 안에 있음**
- 권장하지 않음. `C:\` 실제 로컬 폴더를 별도로 만드세요
- 이유: Drive Desktop이 G:\ 를 가상 드라이브로 관리해서 watchdog 이벤트가 불안정

### 4. 첫 실행 (OAuth 브라우저 인증)

```bash
uv run python main.py
```

브라우저가 자동으로 열립니다:

1. **Google 계정 로그인**
2. **"Google이 확인하지 않은 앱" 경고**:
   - "고급" → "obsidian-sync(으)로 이동" 클릭
   - (테스트 앱이라 정상)
3. **"Drive 파일 보기, 수정, 생성, 삭제" 권한 허용**
4. **"The authentication flow has completed"** 메시지 → 브라우저 닫아도 됨

> `token.json` 이 자동 생성됩니다. **이 기기 전용**이므로 다른 기기로 복사 금지.

**정상 부팅 로그 예시:**
```
[INFO] src.drive_client: Google Drive 서비스 준비 완료
[INFO] src.state: 상태 파일이 없습니다: ...\sync\sync_state.json
[INFO] src.drive_client: Drive 전체 파일 목록: NNNN개
[INFO] src.reconciler: reconciler.run_without_state: 로컬 N개, 원격 M개 → X개 action
... (동기화 진행)
[INFO] src.local_watcher: LocalWatcher 시작: ...
[INFO] src.main: 기동 완료 — shutdown_event 대기
[INFO] src.poller: AdaptivePoller 시작
```

**볼트 규모별 예상 시간:**

| 파일 수 | Drive 목록 | 초기 sync 소요 |
|---|---|---|
| ~1,000 | ~30초 | 2~5분 |
| ~5,000 | ~3~4분 | 15~30분 |
| ~10,000 | ~5~8분 | 30분~1시간 |

중단하지 말고 끝까지 기다리세요. 한번만 겪으면 이후 재시작은 warm start(수 초)입니다.

### 5. 동작 확인

| 테스트 | 기대 |
|---|---|
| 로컬에서 `.md` 파일 수정 | 5~10초 내 Drive 반영 |
| Drive 웹에서 파일 수정 | 30초~1분 내 로컬 반영 |
| 로컬에서 파일 삭제 | Drive 휴지통으로 이동 |

로그 실시간 보기 (cmd):
```cmd
powershell "Get-Content -Path obsidian_sync.log -Wait -Tail 30 -Encoding UTF8"
```

### 6. 백그라운드 자동 실행 등록

정상 동작 확인 후, 위 수동 실행을 **서비스로 전환**합니다. 수동 실행 데몬은 `Ctrl+C`로 중단.

#### Windows — NSSM 서비스

**nssm.exe 준비:**

1. https://nssm.cc/download 에서 `nssm 2.24` 다운로드
2. 압축 해제 → `win64\nssm.exe` 를 프로젝트 루트에 복사
   - (또는 시스템 PATH에 이미 있으면 생략)

**서비스 등록:**

**관리자 권한 cmd** 열기 (중요 — 일반 cmd로는 설치 실패):

```cmd
cd C:\path\to\obsidian_sync
nssm install ObsidianSync
```

GUI 창이 뜨면 3개 필드 입력:

| 필드 | 값 (경로를 본인 기기에 맞게) |
|---|---|
| Path | `C:\path\to\obsidian_sync\.venv\Scripts\python.exe` |
| Arguments | `main.py` |
| Startup directory | `C:\path\to\obsidian_sync` |

→ **Install service** 클릭

**서비스 시작:**
```cmd
nssm start ObsidianSync
nssm status ObsidianSync
```

`SERVICE_RUNNING` 나오면 성공. **이제 PC 재부팅해도 자동 시작됩니다.**

#### Linux — systemd

`/etc/systemd/system/obsidian-sync.service`:

```ini
[Unit]
Description=Obsidian Google Drive Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/obsidian_sync
ExecStart=/home/YOUR_USERNAME/obsidian_sync/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable obsidian-sync
sudo systemctl start obsidian-sync
journalctl -u obsidian-sync -f
```

#### macOS — launchd

`~/Library/LaunchAgents/com.local.obsidian-sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.obsidian-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/obsidian_sync/.venv/bin/python</string>
        <string>main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/obsidian_sync</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/obsidian_sync/obsidian_sync.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/obsidian_sync/obsidian_sync.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.local.obsidian-sync.plist
launchctl start com.local.obsidian-sync
```

---

## 파일별 "복사 가능 여부" 정리

| 파일 | 기기 간 공유 | 이유 |
|---|---|---|
| `credentials.json` | **O** | Google Cloud 프로젝트의 앱 인증 — 모든 기기 동일 |
| `config.yaml` | X | 볼트 경로가 기기마다 다름 |
| `token.json` | **X** | 기기별 Google 로그인 세션 — 첫 실행 시 자동 생성 |
| `.sync/sync_state.json` | **X** | 기기별 동기화 책갈피 — 자동 생성 |
| `.venv/` | X | Python 환경이 OS/경로 의존적 — `uv sync` 재생성 |
| `nssm.exe` | X (Windows만) | 각 기기에서 다운로드 |
| `.gitignore`, `src/`, `tests/`, `pyproject.toml`, `uv.lock` | **O** | 코드·설정은 동일 |

---

## 트러블슈팅

### `credentials.json not found`
- 프로젝트 루트에 배치했는지 확인 (`obsidian_sync/credentials.json`)

### `yaml.scanner.ScannerError`
- `config.yaml` 에 백슬래시(`\`) 있음 → 슬래시(`/`)로 변경
- 경로를 따옴표(`"..."`)로 감쌌으면 제거

### `403 accessNotConfigured`
- Google Cloud Console에서 **Drive API 활성화 안 됨** → API 라이브러리에서 "사용" 클릭

### `403 access_denied` / "액세스 차단됨"
- OAuth 동의 화면의 **테스트 사용자**에 본인 Gmail이 없음 → 추가

### `folder_id not found`
- `folder_id` 값에 URL 전체 들어갔는지 확인 → ID 부분만 남기기

### 인증은 됐는데 동기화 안 됨
- `config.yaml` 의 `watch_paths[0].path` 가 실제 존재하는 폴더인지 확인
- 볼트 안 파일이 모두 `.obsidian/` 또는 `.smart-env/` 등 제외 패턴에 해당하는지 확인

### `token.json` 관련 오류
- 다른 기기의 `token.json` 을 복사해서 사용 중 → 삭제 후 `uv run python main.py` 로 재인증

### 기존 기기가 이미 동기화 중인데 새 기기 추가
- 두 기기 다 같은 `folder_id` 사용하면 **자동으로 양방향 동기화**됨
- 같은 파일을 양쪽에서 동시 수정 시 `.conflict-<device_id>-<시각>.md` 사본 생성 → 수동 정리

### Windows 서비스로 시작은 했는데 로그에 아무것도 안 찍힘
- NSSM GUI에서 **Startup directory** 빠뜨림 → `nssm edit ObsidianSync` 로 수정
- 또는 `token.json` 이 없어서 OAuth 인증이 안 됨 → 먼저 수동 실행 `uv run python main.py` 로 인증 후 서비스 재시작

---

## 체크리스트 (이것만 있으면 됨)

새 기기 설정 완료 전에 확인:

- [ ] `uv sync` 로 `.venv/` 생성됨
- [ ] `credentials.json` 프로젝트 루트에 있음
- [ ] `config.yaml` 에 이 기기 볼트 경로 정확히 입력
- [ ] `config.yaml` 의 `folder_id` 가 기존 기기와 동일
- [ ] 볼트 폴더 안 `.sync/` 없음 (있다면 삭제)
- [ ] `uv run python main.py` 수동 실행 성공 + 브라우저 OAuth 완료
- [ ] 초기 reconcile 종료 후 `LocalWatcher 시작` 로그 확인
- [ ] 로컬 파일 수정 → Drive 반영 확인
- [ ] Drive 파일 수정 → 로컬 반영 확인
- [ ] 수동 실행 종료 후 NSSM (또는 systemd/launchd) 서비스 등록
- [ ] 서비스 상태 `SERVICE_RUNNING` 확인
- [ ] PC 재부팅 후 자동 시작 확인 (선택)

여기까지 모두 체크되면 이 기기도 완전히 통합된 상태입니다.

---

## 다중 볼트 운영 (한 기기에서 여러 볼트 동시 동기화)

볼트 여러 개(업무/개인/공부 등)를 각각 다른 Drive 폴더로 동기화하려면 두 가지 방식 중 선택.

### 방식 1: 프로젝트 폴더 복사 (간단, 2~3개 볼트)

각 볼트당 프로젝트 폴더를 별도로 둔다. `.venv/` 도 폴더마다 독립.

```
C:\obsidian_sync_work\     ← 업무용
C:\obsidian_sync_personal\ ← 개인용
```

위의 [시나리오 B](#시나리오-b-폴더-통째-복사해서-가져오기) 절차를 각 폴더에 반복. **각 config.yaml의 `folder_id` 와 `watch_paths.path` 는 반드시 서로 달라야** 함.

서비스 이름도 폴더마다 다르게:
```cmd
nssm install ObsidianSyncWork
nssm install ObsidianSyncPersonal
```

### 방식 2: 한 프로젝트 폴더 + `--config` 인자 (효율적, 4개 이상 권장)

하나의 프로젝트 폴더에 설정 파일을 여러 개 두고, 실행 시 `--config` 로 지정.

```
C:\01.project\obsidian_sync\
├── .venv\                   ← 1개만
├── src\
├── credentials.json         ← 공유
├── config.yaml              ← 기본 (업무용)
├── config-personal.yaml     ← 추가
├── token.json               ← 업무용 OAuth 토큰
└── token-personal.json      ← 개인용 OAuth 토큰
```

각 설정 파일은 `token_file` 도 **서로 다르게** 지정:

```yaml
# config.yaml (업무)
watch_paths:
  - path: C:/obsidian_work
    hooks: [sync]
drive:
  credentials_file: credentials.json
  token_file: token.json            ← 업무용
  folder_id: 1AAAA...

# config-personal.yaml
watch_paths:
  - path: C:/obsidian_personal
    hooks: [sync]
drive:
  credentials_file: credentials.json
  token_file: token-personal.json   ← 개인용 (다른 경로)
  folder_id: 2BBBB...
```

실행:
```bash
uv run python main.py --config config.yaml             # 업무 볼트
uv run python main.py --config config-personal.yaml    # 개인 볼트
```

첫 실행 시 각 config마다 브라우저 OAuth 인증이 한 번씩 필요. 끝나면 해당 `token_file` 자동 생성.

**NSSM 서비스 등록** (각 config당 하나씩, 모두 같은 python 사용):

| 서비스명 | Path | Arguments | Startup dir |
|---|---|---|---|
| ObsidianSyncWork | `C:\01.project\obsidian_sync\.venv\Scripts\python.exe` | `main.py --config config.yaml` | `C:\01.project\obsidian_sync` |
| ObsidianSyncPersonal | `C:\01.project\obsidian_sync\.venv\Scripts\python.exe` | `main.py --config config-personal.yaml` | `C:\01.project\obsidian_sync` |

```cmd
nssm install ObsidianSyncWork       # GUI에 위 값 입력
nssm install ObsidianSyncPersonal
nssm start ObsidianSyncWork
nssm start ObsidianSyncPersonal
```

### 다중 볼트 운영 시 절대 원칙

- [ ] 각 config의 **`folder_id` 가 서로 달라야** 함 (같으면 크로스 동기화 재앙)
- [ ] 각 config의 **`watch_paths.path` 가 서로 달라야** 함 (같은 로컬 폴더 이중 감시 시 에코 폭발)
- [ ] 각 config의 **`token_file` 이 서로 달라야** 함 (같으면 서로의 토큰 덮어씀)
- [ ] 로그 파일(`logging.file`)도 분리 권장 (`obsidian_sync_work.log` 등)
- [ ] NSSM 서비스 이름 서로 달라야 함

---

## 참고

- 프로젝트 개요: [../README.md](../README.md)
- 처음 설정하는 사용자를 위한 입문 가이드: [../SETUP_GUIDE.md](../SETUP_GUIDE.md)
- 설계 사양: [../obsidian-vault-sync-spec.md](../obsidian-vault-sync-spec.md)
- 다른 개발자를 위한 재사용 가이드: [../templates/gdrive-watchdog-sync.md](../templates/gdrive-watchdog-sync.md)
