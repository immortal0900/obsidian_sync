# Obsidian Sync — 새 기기 설정 가이드

> 다른 PC, 노트북, 서버 등에서 Obsidian Sync를 추가로 설정하는 단계별 가이드입니다.
> 처음부터 설정하는 분도 이 가이드를 따르면 됩니다.

---

## 시작하기 전에 확인할 것

**이미 다른 기기에서 동작 중이라면:**
- 기존 기기의 `credentials.json` 파일 (복사해서 쓸 수 있음)
- 기존 기기의 `config.yaml`에 적힌 `folder_id` 값

**아예 처음 설정하는 거라면:**
- 이 가이드 맨 아래 "부록: Google Cloud 프로젝트 처음 만들기"를 먼저 진행하세요.
- 거기서 `credentials.json`과 `folder_id`를 얻을 수 있습니다.

---

## 1단계: 프로젝트 다운로드

```bash
git clone <repo-url>
cd obsidian_sync
```

---

## 2단계: uv 설치 + 의존성 설치

uv는 Python 패키지 매니저입니다. 이미 설치되어 있다면 건너뛰세요.

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

설치 후 의존성 설치:

```bash
uv sync
```

---

## 3단계: credentials.json 넣기

`credentials.json`은 "이 프로그램이 Google Drive에 접근해도 됩니다"라는 허가증 같은 파일입니다.
**모든 기기에서 같은 파일을 사용합니다.**

### 방법 A: 기존 기기에서 복사 (권장)

기존 기기의 프로젝트 폴더에서 `credentials.json`을 찾아서 새 기기로 복사합니다.
USB 드라이브, 이메일 첨부, 또는 아무 방법이나 사용하세요.

```
[기존 기기]  obsidian_sync/credentials.json
                     ↓  복사
[새 기기]    obsidian_sync/credentials.json
```

### 방법 B: Google Cloud Console에서 다시 다운로드

1. [console.cloud.google.com](https://console.cloud.google.com) 접속
2. 기존에 만든 프로젝트 선택
3. 왼쪽 메뉴 → **API 및 서비스 → 클라이언트**
4. 기존에 만든 OAuth 클라이언트 클릭
5. **JSON 다운로드** → 파일명을 `credentials.json`으로 변경
6. 프로젝트 루트 폴더에 넣기

---

## 4단계: config.yaml 작성

설정 템플릿을 복사합니다:

```bash
cp config.example.yaml config.yaml      # Linux/macOS
copy config.example.yaml config.yaml    # Windows
```

`config.yaml`을 열고 아래 3곳을 수정합니다:

```yaml
watch_paths:
  - path: C:/Users/YourName/ObsidianVault   # (1) 이 기기의 볼트 폴더 경로
    hooks: [sync]

drive:
  credentials_file: credentials.json
  token_file: token.json
  folder_id: 1aBcDeFgHiJkLmNoPqRsT          # (2) Drive 폴더 ID

sync:
  debounce_seconds: 5                        # (3) 필요하면 조정
  poll_interval_seconds: 60
  delete_local: false
```

### 자주 하는 실수

| 실수 | 올바른 예 |
|------|-----------|
| 경로에 백슬래시(`\`) 사용 | `C:/obsidian_world` (슬래시 `/` 사용) |
| 경로를 따옴표로 감싸기 | `path: C:/obsidian_world` (따옴표 없이) |
| folder_id에 URL 전체 입력 | `folder_id: 1K1sGu96jsYawdJnJ9czj` (ID 부분만) |

### Drive 폴더 ID는 어디서 찾나요?

**기존 기기가 있다면:** 기존 기기의 `config.yaml`을 열어서 `folder_id` 값을 복사하세요.

**없다면:** Google Drive를 브라우저에서 열고, 동기화할 폴더로 이동한 뒤 주소창에서 복사:

```
https://drive.google.com/drive/folders/1K1sGu96jsYawdJnJ9czj
                                        └── 이 부분만 복사해서 folder_id에 붙여넣기
```

---

## 5단계: 첫 실행 (브라우저 인증)

```bash
uv run python main.py
```

실행하면 브라우저가 자동으로 열립니다. 아래 순서대로 진행하세요:

1. **Google 계정으로 로그인**
2. **"Google이 확인하지 않은 앱" 경고가 나오면:**
   - "고급" 클릭 → "obsidian-sync(으)로 이동" 클릭
   - (이 경고는 정상입니다. 우리가 만든 앱이 Google 심사를 안 받았기 때문)
3. **"Drive 파일 보기, 수정, 생성, 삭제" 권한 허용**
4. **"The authentication flow has completed" 메시지** → 브라우저를 닫아도 됩니다

터미널에 아래처럼 나오면 성공:

```
=== Obsidian Sync Daemon starting ===
Google Drive service ready
Drive polling initialized (start token: ...)
Watching C:\obsidian_world with hooks: ['sync']
Watcher started (1 path(s))
Daemon running. Press Ctrl+C to stop.
```

> 이때 `token.json`이 자동 생성됩니다.
> 이 파일은 **이 기기 전용**이므로 다른 기기로 복사하면 안 됩니다.

---

## 6단계: 동작 확인

| 확인 항목 | 방법 |
|-----------|------|
| 로컬 → Drive | 볼트에서 아무 파일 수정 → 5초 후 Google Drive에서 반영 확인 |
| Drive → 로컬 | Google Drive에서 파일 수정 → 1분 내 로컬 폴더에서 확인 |
| 로그 | `obsidian_sync.log` 파일을 열어보기 |

---

## 기기별 파일 정리 (어떤 파일을 공유하고, 어떤 걸 따로 만드나?)

| 파일 | 기기 간 공유 | 왜? |
|------|:-----------:|------|
| `credentials.json` | O (같은 파일) | Google Cloud 프로젝트의 앱 인증서 — 모든 기기 동일 |
| `config.yaml` | X (기기마다 작성) | 볼트 폴더 경로가 기기마다 다름 |
| `token.json` | X (자동 생성) | 기기별 Google 로그인 토큰 — 첫 실행 시 자동 생성 |
| `drive_id_cache.json` | X (자동 생성) | 파일-Drive ID 매핑 캐시 — 실행 중 자동 생성 |
| `.sync/sync_state.json` | X (볼트 안에 생성) | 동기화 상태 파일 — 볼트 폴더 안에 자동 생성 |

> 위 파일은 모두 `.gitignore`에 포함되어 있어서 `git push`해도 올라가지 않습니다.

---

## 백그라운드 실행 (선택사항)

터미널을 켜놓지 않아도 동기화가 계속 되게 하려면:

### Windows — 콘솔 창 없이 실행

```bash
uv run pythonw main.py
```

### Windows — 부팅 시 자동 실행 (NSSM 서비스)

NSSM은 일반 프로그램을 Windows 서비스로 만들어주는 도구입니다.
한 번 등록하면 PC를 켤 때마다 자동으로 시작됩니다.

1. [nssm.cc/download](https://nssm.cc/download)에서 `nssm 2.24` 다운로드
2. 압축 해제 → `win64\nssm.exe`를 프로젝트 폴더에 넣기
3. **관리자 권한 터미널**을 열고:

```bash
cd C:\01.project\obsidian_sync
nssm install ObsidianSync
```

4. GUI 창이 열리면:

| 항목 | 입력할 값 |
|------|----------|
| Path | `.venv\Scripts\python.exe` |
| Arguments | `main.py` |
| Startup directory | `C:\01.project\obsidian_sync` |

5. **Install service** 클릭
6. 시작:

```bash
nssm start ObsidianSync
```

서비스 관리:
```bash
nssm status ObsidianSync      # 상태 확인
nssm restart ObsidianSync     # 재시작
nssm stop ObsidianSync        # 중지
nssm remove ObsidianSync      # 서비스 삭제 (완전 제거)
```

### Linux / macOS — systemd

`/etc/systemd/system/obsidian-sync.service` 파일을 만듭니다:

```ini
[Unit]
Description=Obsidian Google Drive Sync
After=network.target

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/obsidian_sync
ExecStart=/path/to/obsidian_sync/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable obsidian-sync   # 부팅 시 자동 시작 등록
systemctl start obsidian-sync    # 지금 바로 시작
journalctl -u obsidian-sync -f   # 로그 실시간 보기
```

---

## 문제 해결

| 이런 에러가 나오면 | 원인 | 이렇게 해결 |
|-------------------|------|-------------|
| `yaml.scanner.ScannerError` | config.yaml 경로에 `\`나 `"` 사용 | 슬래시(`/`)로 바꾸고 따옴표 제거 |
| `403 accessNotConfigured` | Google Drive API가 꺼져 있음 | Cloud Console → Drive API → "사용" 클릭 |
| `403 access_denied` "액세스 차단됨" | Gmail이 테스트 사용자에 안 들어감 | OAuth 동의 화면 → 대상 → 본인 Gmail 추가 |
| `folder_id` 관련 오류 | folder_id에 URL 전체를 넣음 | ID 부분만 남기고 나머지 삭제 |
| `credentials.json not found` | 인증 파일이 없음 | 기존 기기에서 복사하거나 Cloud Console에서 다시 다운로드 |
| 인증은 됐는데 동기화 안 됨 | 볼트 경로가 틀림 | config.yaml의 `path`가 실제 폴더 위치와 일치하는지 확인 |
| `token.json` 관련 오류 | 다른 기기의 token.json을 복사해서 씀 | token.json 파일을 삭제하고 다시 실행 (브라우저 재인증) |

---

## 부록: Google Cloud 프로젝트 처음 만들기

> 이미 다른 기기에서 설정을 완료했다면 이 섹션은 건너뛰세요.
> `credentials.json`을 기존 기기에서 복사하면 됩니다.

### A-1. Google Cloud 프로젝트 생성 + Drive API 활성화

1. [console.cloud.google.com](https://console.cloud.google.com) 에 접속
2. 상단의 프로젝트 선택 드롭다운 → **새 프로젝트** → 이름: `obsidian-sync` → 만들기
3. 새 프로젝트가 선택된 상태에서, 왼쪽 메뉴 → **API 및 서비스 → 라이브러리**
4. 검색창에 `Google Drive API` 입력 → 클릭 → **사용** 버튼 클릭

### A-2. OAuth 동의 화면 설정

이 단계는 "이 앱이 뭔지" Google에 알려주는 과정입니다.

1. 왼쪽 메뉴 → **API 및 서비스 → OAuth 동의 화면** → **시작하기**
2. 앱 정보 입력:
   - 앱 이름: `obsidian-sync`
   - 사용자 지원 이메일: 본인 Gmail
3. 대상: **외부** 선택
4. 연락처 정보: 본인 Gmail 입력
5. 저장 후, 왼쪽 메뉴에서 **대상** → **사용자 추가** → 본인 Gmail 등록

> **중요**: 본인 Gmail을 테스트 사용자로 등록하지 않으면 나중에 "액세스 차단됨" 오류가 납니다.

### A-3. OAuth 2.0 클라이언트 ID 생성

이 단계에서 `credentials.json` 파일을 얻습니다.

1. 왼쪽 메뉴 → **클라이언트** → **+ 클라이언트 만들기**
2. 애플리케이션 유형: **데스크톱 앱**
3. 이름: `obsidian-sync`
4. **만들기** 클릭
5. **JSON 다운로드** 클릭 → 파일명을 `credentials.json`으로 변경
6. 프로젝트 폴더(`obsidian_sync/`)에 넣기

이제 위의 "1단계"부터 다시 진행하면 됩니다.
