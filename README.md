# obsidian-sync

로컬 Obsidian 볼트와 Google Drive 간 양방향 자동 동기화 데몬.

- **로컬→Drive**: `watchdog`으로 파일 변경 감지 → Drive API로 업로드 (디바운싱 5초)
- **Drive→로컬**: Changes API를 1분 간격 폴링 → 변경분 다운로드
- **충돌 해결**: last-write-wins (마지막 수정 시간 기준)
- **확장 가능**: 훅 시스템으로 Phase 2(blog_convert), Phase 3(llm_tagging) 추가 예정

---

## 요구사항

- Python 3.12
- [uv](https://docs.astral.sh/uv/) 패키지 매니저

---

## 설치

### 1. uv 설치 (미설치 시)

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 코드 받기

```bash
git clone <repo-url>
cd obsidian_sync
```

### 3. 의존성 설치

```bash
uv sync
```

---

## Google Drive 설정

### 1. Google Cloud Console

1. [console.cloud.google.com](https://console.cloud.google.com) → 새 프로젝트 생성 (예: `obsidian-sync`)
2. **APIs & Services → Library** → `Google Drive API` 검색 → **Enable**

### 2. OAuth 동의 화면

1. **APIs & Services → OAuth consent screen**
2. User type: **External**
3. App name: `Obsidian Sync`, 본인 Gmail을 Test user로 추가
4. Scopes: `https://www.googleapis.com/auth/drive` 추가

### 3. 인증 정보 생성

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Desktop app**
3. 생성 후 JSON 다운로드 → `credentials.json`으로 이름 변경 → 프로젝트 루트에 복사

### 4. Drive 폴더 ID 확인

Google Drive 브라우저에서 동기화할 폴더 열기 → URL에서 ID 복사:
```
https://drive.google.com/drive/folders/[여기가_folder_id]
```

---

## 설정

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 편집:

```yaml
watch_paths:
  - path: C:/Users/YourName/ObsidianVault   # 실제 볼트 경로
    hooks: [sync]

drive:
  credentials_file: credentials.json
  token_file: token.json
  folder_id: YOUR_GOOGLE_DRIVE_FOLDER_ID

sync:
  debounce_seconds: 5
  poll_interval_seconds: 60
  delete_local: false   # true로 설정 시 Drive 삭제가 로컬에도 반영됨
```

---

## 실행

### 첫 실행 (OAuth 인증)

```bash
uv run python main.py
```

브라우저가 열리면 Google 계정으로 로그인 → Drive 접근 허용 → `token.json` 자동 생성.

이후 실행부터는 브라우저 없이 자동 시작됩니다.

### 백그라운드 실행 (콘솔 창 없음)

```bash
uv run pythonw main.py
```

---

## 데몬 등록 (부팅 시 자동 실행)

### Windows — 작업 스케줄러

1. 작업 스케줄러 열기 → **작업 만들기** (기본 작업 아님)
2. **일반** 탭: "사용자의 로그온 여부에 관계없이 실행", "가장 높은 수준의 권한으로 실행"
3. **트리거** 탭: **시작할 때**
4. **동작** 탭:
   - 프로그램: `C:\01.project\obsidian_sync\.venv\Scripts\pythonw.exe`
   - 인수: `C:\01.project\obsidian_sync\main.py`
   - 시작 위치: `C:\01.project\obsidian_sync`
5. **설정** 탭: "작업이 실패하면 다시 시작", 1분 간격, 최대 3회

### Windows — NSSM (Windows 서비스)

[nssm.cc](https://nssm.cc/download)에서 `nssm.exe` 다운로드 후:

```cmd
nssm install ObsidianSync
```

GUI에서:
- Path: `.venv\Scripts\python.exe`
- Arguments: `main.py`
- Startup directory: `C:\01.project\obsidian_sync`

```cmd
nssm start ObsidianSync
```

### Linux / macOS — systemd

`/etc/systemd/system/obsidian-sync.service` 생성:

```ini
[Unit]
Description=Obsidian Google Drive Sync Daemon
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
systemctl enable obsidian-sync
systemctl start obsidian-sync
journalctl -u obsidian-sync -f   # 로그 확인
```

---

## 새 기기에서 설정

```bash
git clone <repo-url>
cd obsidian_sync
uv sync
cp config.example.yaml config.yaml
# config.yaml 편집 (볼트 경로, folder_id)
# credentials.json 복사 (Google Cloud Console에서 재발급 가능)
uv run python main.py   # 첫 실행: 브라우저 OAuth 인증
```

> `token.json`, `config.yaml`, `credentials.json`은 `.gitignore`에 포함되어 있으므로 기기마다 별도 생성해야 합니다.

---

## 로그 확인

```bash
tail -f obsidian_sync.log
```

로그 파일은 5MB마다 교체되며 최대 3개 보관됩니다.

---

## 프로젝트 구조

```
obsidian_sync/
├── core/
│   ├── watcher.py       # watchdog 감지 + 디바운스
│   └── drive_sync.py    # Google Drive API 래퍼 + 폴링
├── hooks/
│   ├── __init__.py      # 훅 레지스트리
│   └── sync_hook.py     # ChangeEvent, BaseHook, SyncHook
├── main.py              # 데몬 진입점
├── config.example.yaml  # 설정 템플릿
└── README.md
```
