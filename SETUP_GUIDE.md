# Obsidian Sync 설정 가이드

> 로컬 Obsidian 볼트 ↔ Google Drive 양방향 자동 동기화 데몬 설치 및 설정 가이드

---

## 1. 프로젝트 설치

```bash
git clone <repo-url>
cd obsidian_sync
uv sync
```

> uv 미설치 시: `pip install uv` 또는 [docs.astral.sh/uv](https://docs.astral.sh/uv/) 참고

---

## 2. Google Cloud Console 설정

### 2-1. Google Drive API 활성화

1. [console.cloud.google.com](https://console.cloud.google.com) 접속
2. 상단 드롭다운에서 프로젝트 선택 (기존 프로젝트 사용 가능)
3. 왼쪽 메뉴 → **API 및 서비스 → 라이브러리**
4. `Google Drive API` 검색 → **사용** 클릭

### 2-2. OAuth 동의 화면 설정

1. 왼쪽 메뉴 → **API 및 서비스 → OAuth 동의 화면**
2. **시작하기** 클릭
3. 앱 정보:
   - 앱 이름: `obsidian-sync`
   - 사용자 지원 이메일: 본인 Gmail 선택
4. 대상: **외부** 선택
5. 연락처 정보: 본인 Gmail 입력
6. 완료 후 왼쪽 메뉴 → **대상** → **사용자 추가** → 본인 Gmail 등록

> 본인 Gmail이 테스트 사용자에 등록되지 않으면 "액세스 차단됨" 403 오류 발생

### 2-3. OAuth 2.0 클라이언트 ID 생성

1. 왼쪽 메뉴 → **클라이언트** → **+ 클라이언트 만들기**
2. 애플리케이션 유형: **데스크톱 앱**
3. 이름: `obsidian-sync`
4. **만들기** → **JSON 다운로드**
5. 파일명을 `credentials.json`으로 변경 → 프로젝트 루트에 복사:

```
C:\01.project\obsidian_sync\credentials.json
```

---

## 3. config.yaml 작성

```bash
copy config.example.yaml config.yaml
```

`config.yaml` 편집:

```yaml
watch_paths:
  - path: C:/Users/YourName/ObsidianVault   # 실제 볼트 경로 (슬래시 / 사용)
    hooks: [sync]

drive:
  credentials_file: credentials.json
  token_file: token.json
  folder_id: 1aBcDeFgHiJkLmNoPqRsT          # Drive 폴더 ID만 입력

sync:
  debounce_seconds: 5
  poll_interval_seconds: 60
  delete_local: false
```

### 주의사항

| 항목 | 올바른 예 | 잘못된 예 |
|------|-----------|-----------|
| 경로 | `C:/obsidian_world` | `"C:\obsidian_world"` (백슬래시 + 따옴표 사용 금지) |
| folder_id | `1K1sGu96jsYawdJnJ9czj` | `https://drive.google.com/drive/folders/1K1sGu96jsYawdJnJ9czj` (URL 전체 입력 금지) |

### Drive 폴더 ID 확인 방법

Google Drive에서 동기화할 폴더 열기 → 주소창 URL:

```
https://drive.google.com/drive/folders/1K1sGu96jsYawdJnJ9czj
                                        └── 이 부분만 복사
```

---

## 4. 첫 실행 (OAuth 인증)

```bash
cd C:\01.project\obsidian_sync
python3 -m uv run python main.py
```

1. 브라우저가 자동으로 열림
2. Google 계정 로그인
3. "Google이 확인하지 않은 앱" 경고 → **고급** → **obsidian-sync(으)로 이동** 클릭
4. Drive 접근 **허용**
5. "The authentication flow has completed. You may close this window." 메시지 → 브라우저 닫기

성공 시 터미널 출력:

```
=== Obsidian Sync Daemon starting ===
Google Drive service ready
Drive polling initialized (start token: ...)
Watching C:\obsidian_world with hooks: ['sync']
Watcher started (1 path(s))
Daemon running. Press Ctrl+C to stop.
```

> `token.json`이 자동 생성됨. 이후 실행부터는 브라우저 없이 바로 시작.

---

## 5. Windows 서비스 등록 (NSSM) — 부팅 시 자동 실행

터미널이 닫혀도, 로그인하지 않아도 백그라운드에서 자동 실행됩니다.

### 5-1. NSSM 설치

1. [nssm.cc/download](https://nssm.cc/download) 에서 `nssm 2.24` 다운로드
2. 압축 해제 → `win64\nssm.exe` 를 프로젝트 폴더에 복사:

```
C:\01.project\obsidian_sync\nssm.exe
```

### 5-2. 서비스 등록

**관리자 권한 터미널** (시작 메뉴 → `cmd` 검색 → 관리자 권한으로 실행):

```bash
cd C:\01.project\obsidian_sync
nssm install ObsidianSync
```

GUI 창에서:

| 항목 | 값 |
|------|-----|
| Path | `C:\01.project\obsidian_sync\.venv\Scripts\python.exe` |
| Arguments | `main.py` |
| Startup directory | `C:\01.project\obsidian_sync` |

→ **Install service** 클릭

### 5-3. 서비스 시작

```bash
nssm start ObsidianSync
```

출력: `ObsidianSync: START: 작업을 완료했습니다.`

### 5-4. 서비스 관리 명령어

```bash
nssm status ObsidianSync    # 상태 확인
nssm stop ObsidianSync      # 중지
nssm start ObsidianSync     # 시작
nssm restart ObsidianSync   # 재시작
nssm remove ObsidianSync    # 서비스 삭제
```

---

## 6. 새 기기에서 설정

```bash
git clone <repo-url>
cd obsidian_sync
uv sync
copy config.example.yaml config.yaml
# config.yaml 편집 (볼트 경로, folder_id)
# credentials.json 복사 (Google Cloud Console에서 재다운로드 가능)
python3 -m uv run python main.py    # 첫 실행: 브라우저 OAuth 인증
```

> `token.json`, `config.yaml`, `credentials.json`은 `.gitignore`에 포함 — 기기마다 별도 생성

---

## 7. 동작 확인

| 테스트 | 방법 |
|--------|------|
| 로컬→Drive | 볼트 폴더에서 파일 생성/수정 → 5초 후 Google Drive 확인 |
| Drive→로컬 | Google Drive에서 파일 수정 → 1분 내 로컬 반영 확인 |
| 로그 확인 | `obsidian_sync.log` 확인 또는 `type obsidian_sync.log` |

---

## 8. 문제 해결

| 증상 | 원인 | 해결 |
|------|------|------|
| `yaml.scanner.ScannerError` | config.yaml에 백슬래시 또는 따옴표 경로 | 슬래시(`/`) 사용, 따옴표 제거 |
| `403 accessNotConfigured` | Google Drive API 미활성화 | Cloud Console에서 Drive API 활성화 |
| `403 access_denied` "액세스 차단됨" | 테스트 사용자 미등록 | OAuth 동의 화면 → 대상 → 본인 Gmail 추가 |
| `folder_id` 오류 | URL 전체를 folder_id에 입력 | 폴더 ID 부분만 복사 |
| `credentials.json not found` | 인증 파일 없음 | Cloud Console에서 JSON 다운로드 → 프로젝트 루트에 복사 |
