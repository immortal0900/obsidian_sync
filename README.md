# obsidian-sync

로컬 Obsidian 볼트와 Google Drive 간 양방향 자동 동기화 프로그램.
Obsidian이 꺼져 있어도 동작하는 독립 데몬입니다.

### 무엇을 하나요?

```
Obsidian 볼트 (내 PC)           Google Drive (클라우드)
       │                               │
       │  ── 파일 수정하면 자동 업로드 ──→  │
       │                               │
       │  ←── 다른 기기에서 바뀐 파일 다운로드 ──  │
       │                               │
       │  ── 파일 삭제하면 반대쪽도 삭제 ──→  │
       │  ←──────────────────────────  │
```

- **로컬→Drive**: 파일 변경을 실시간 감지(`watchdog`) → 디바운싱 후 업로드
- **Drive→로컬**: 적응형 폴링(10초~2분)으로 클라우드 변경 감지 → 다운로드
- **충돌 시**: 양쪽 모두 보존 — `.sync-conflict-{시각}-{기기}.{확장자}` 사본이 생성되어 내용이 절대 날아가지 않음 (Syncthing 명명 규칙 호환)
- **삭제 전파**: Version Vector 기반 3-way 판정으로 한쪽 삭제를 안전하게 반대쪽으로 전파. 삭제된 파일은 `.sync/trash/` 또는 Drive `.sync/tombstones/` 로 이동되어 복구 가능 (기본 30일/90일 보관)

---

## 빠른 시작 (5분)

> 처음 설정하는 분은 [SETUP_GUIDE.md](SETUP_GUIDE.md)에 스크린샷 포함 상세 가이드가 있습니다.

### 1단계: 설치

```bash
git clone <repo-url>
cd obsidian_sync
uv sync                  # 의존성 설치 (uv 미설치 시 아래 참고)
```

<details>
<summary>uv가 없다면?</summary>

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
</details>

### 2단계: Google Drive 연동

1. [Google Cloud Console](https://console.cloud.google.com)에서 프로젝트 생성 + Drive API 활성화
2. OAuth 클라이언트 ID (데스크톱 앱) 생성 → JSON 다운로드 → `credentials.json`으로 저장 (프로젝트 폴더에 두기)
3. Google Drive에서 동기화할 폴더의 ID 복사:
   ```
   https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsT
                                            └── 이 부분이 folder_id
   ```

> 자세한 절차는 [SETUP_GUIDE.md](SETUP_GUIDE.md)의 "부록: Google Cloud 프로젝트 최초 설정" 참고

### 3단계: 설정 파일 작성

```bash
cp config.example.yaml config.yaml
```

`config.yaml`을 열고 3가지만 수정:

```yaml
watch_paths:
  - path: C:/Users/YourName/ObsidianVault   # <-- 내 볼트 경로
    hooks: [sync]

drive:
  credentials_file: credentials.json
  token_file: token.json
  folder_id: YOUR_FOLDER_ID                  # <-- 위에서 복사한 ID

sync:
  debounce_seconds: 5        # 파일 저장 후 5초 기다렸다가 업로드
  poll_interval_seconds: 60  # 클라우드 변경을 60초마다 확인
  # v2 참고: 삭제는 항상 .sync/trash(로컬) / .sync/tombstones(Drive)로
  # 이동되며 30/90일 후 자동 정리. delete_local 옵션은 더 이상 사용되지 않음.

logging:
  level: INFO
  file: obsidian_sync.log
  max_bytes: 5242880   # 5 MB, 초과 시 rotate
  backup_count: 3      # 최대 3개 보관
```

### 4단계: 실행 (아래 3가지 중 택1)

**A. 수동 실행** (테스트 · 첫 인증용 — 브라우저가 열림):

```bash
uv run python -m src.main --config config.yaml
```

**B. 자동 재시작 래퍼** (권장 — 크래시 시 5초 후 자동 재시작):

```bash
uv run python run_forever.py config.yaml
```

**C. Windows 서비스** (부팅 시 자동 시작 · 로그아웃해도 지속) — 아래 "백그라운드 운영" 참고

첫 실행 시 브라우저가 열립니다 → Google 로그인 → Drive 접근 허용 → 완료!
이후부터는 브라우저 없이 바로 시작됩니다.

```
=== Obsidian Sync Daemon starting ===
Google Drive service ready
기동 완료 — shutdown_event 대기
```

**Ctrl+C**로 정상 종료.

---

## 자주 쓰는 명령어 (Cheatsheet)

### 실행

| 용도 | 명령 |
|---|---|
| 수동 실행 (메인 볼트) | `uv run python -m src.main --config config.yaml` |
| 수동 실행 (블로그 볼트) | `uv run python -m src.main --config config_blog.yaml` |
| 자동 재시작 래퍼 | `uv run python run_forever.py config.yaml` |
| 콘솔 창 없이 (Windows) | `uv run pythonw run_forever.py config.yaml` |

### Windows 서비스 제어 (NSSM 설치 후)

| 용도 | 명령 |
|---|---|
| 서비스 상태 확인 | `sc query ObsidianSync` / `sc query ObsidianSyncBlog` |
| 서비스 GUI 관리 | `services.msc` |
| 시작 / 정지 / 재시작 | `nssm start ObsidianSync` / `nssm stop ObsidianSync` / `nssm restart ObsidianSync` |
| (블로그 서비스) | `nssm start ObsidianSyncBlog` 등 |
| 살아있는 프로세스 확인 | `tasklist \| findstr python.exe` |
| 서비스 등록 (관리자) | `install_service.bat` 우클릭 → "관리자 권한으로 실행" |
| 서비스 제거 (관리자) | `uninstall_service.bat` 우클릭 → "관리자 권한으로 실행" |
| 계정 문제 해결 (관리자) | `fix_service_localsystem.bat` 우클릭 → "관리자 권한으로 실행" |

### 로그 확인

| 용도 | 명령 |
|---|---|
| 메인 앱 로그 (tail) | `tail -f obsidian_sync.log` (bash) / `Get-Content obsidian_sync.log -Wait` (PowerShell) |
| 블로그 앱 로그 | `tail -f obsidian_sync_blog.log` |
| 서비스 stdout | `tail -f service_stdout.log` (메인) / `service_stdout_blog.log` (블로그) |
| 서비스 stderr | `tail -f service_stderr.log` / `service_stderr_blog.log` |
| 래퍼 재시작 로그 | `tail -f run_forever.log` |
| 최근 에러만 | `grep -i "error\|traceback" obsidian_sync.log \| tail -20` |
| 삭제 이벤트만 | `grep "delete_" obsidian_sync.log \| tail -20` |

### 개발 · 테스트

| 용도 | 명령 |
|---|---|
| 개발 의존성 설치 | `uv sync --extra dev` |
| 테스트 전체 | `uv run python -m pytest tests/ -v` |
| 특정 테스트 파일 | `uv run python -m pytest tests/test_version_vector.py -v` |
| 정적 분석 (ruff) | `uv run ruff check src/ tests/` |
| 자동 수정 | `uv run ruff check --fix src/ tests/` |

기준치: **458 passed, 2 skipped, 0 failures** + `ruff check` 통과.

---

## 백그라운드 운영

실시간으로 편집을 따라잡으려면 데몬을 24/7 켜두는 것이 좋습니다. 3단계 방어가 내장돼 있어 안전합니다:

1. **앱 내부** — Intent Log(WAL)로 부분 실패 복구
2. **`run_forever.py`** — 프로세스 크래시 시 5초 후 자동 재시작
3. **NSSM 서비스** — OS 레벨 재시작 + 부팅 자동 시작

### Windows — NSSM 이중 서비스 (원클릭)

**전제조건**: `nssm` 설치 필요

```powershell
# 관리자 PowerShell
choco install nssm   # Chocolatey
# 또는 scoop install nssm
```

**설치**:

1. 파일 탐색기에서 [install_service.bat](install_service.bat) 우클릭 → **"관리자 권한으로 실행"**
2. 프롬프트: `Use your account (<user>)? [Y/N]:` → **N** (LocalSystem 권장 — 아래 참고)
3. 두 서비스(`ObsidianSync`, `ObsidianSyncBlog`) 자동 등록 + 시작

**LocalSystem을 쓰는 이유**: `credentials.json` 과 `token.json` 이 **프로젝트 폴더 안**에 있어서 LocalSystem도 접근 가능합니다. 사용자 계정으로 하려면 Windows 로그인 패스워드가 필요한데, Microsoft 계정 + Hello PIN 환경에서는 로그온 실패합니다 (PIN은 서비스 로그온에 사용 불가).

이미 사용자 계정으로 설치했는데 로그온 실패한다면:

```
fix_service_localsystem.bat 우클릭 → 관리자 권한으로 실행
```

### Linux/macOS — systemd

`/etc/systemd/system/obsidian-sync.service`:

```ini
[Unit]
Description=Obsidian Google Drive Sync
After=network-online.target

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/obsidian_sync
ExecStart=/path/to/obsidian_sync/.venv/bin/python run_forever.py config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable obsidian-sync && systemctl start obsidian-sync
systemctl status obsidian-sync
journalctl -u obsidian-sync -f    # 로그 tail
```

---

## 여러 볼트 동시 운영 (선택)

메인 옵시디언 볼트 외에도 **블로그 콘텐츠 폴더** 같은 추가 볼트를 동시 동기화할 수 있습니다. 설정 파일과 서비스를 분리해 **완전 독립 운영**됩니다.

### 구성

```bash
cp config.example.yaml config_blog.yaml
```

`config_blog.yaml` 수정:

```yaml
watch_paths:
  - path: C:/1.Project/quartz/content    # <-- 블로그용 폴더
    hooks: [sync]

drive:
  credentials_file: credentials.json     # 메인과 공유 가능 (동일 Google 계정)
  token_file: token.json                 # 메인과 공유 가능
  folder_id: DIFFERENT_FOLDER_ID         # 반드시 다른 Drive 폴더

logging:
  file: obsidian_sync_blog.log           # 로그는 분리
  # 나머지는 config.yaml과 동일
```

### 실행 — 서비스 2개 병렬

`install_service.bat`이 자동으로 **두 서비스**(`ObsidianSync`, `ObsidianSyncBlog`)를 등록합니다. 두 볼트가 서로 영향 주지 않으며, 한쪽이 죽어도 다른 쪽은 계속 돌아갑니다.

**원칙**:

| 규칙 | 어기면 |
|---|---|
| 각 config의 `folder_id` 는 서로 다를 것 | 같은 Drive 폴더를 두 데몬이 가리키면 충돌 폭발 |
| 각 config의 로컬 볼트 경로 서로 다를 것 | 같은 폴더 이중 감시 → 에코 루프 |
| 각 config의 `file` (로그) 서로 다를 것 | 로그 뒤섞여 디버깅 불가 |

---

## 다른 기기 추가

이미 한 기기에서 동작 중이고, 다른 PC/노트북/서버를 추가하려면:

```bash
git clone <repo-url> && cd obsidian_sync && uv sync
cp config.example.yaml config.yaml
# config.yaml 편집 (이 기기의 볼트 경로 + 같은 folder_id)
# credentials.json 복사 (기존 기기에서 가져오거나 Cloud Console에서 재다운로드)
uv run python -m src.main --config config.yaml   # 브라우저 OAuth 인증
```

단계별 상세: [SETUP_GUIDE.md](SETUP_GUIDE.md), [docs/new-device-setup.md](docs/new-device-setup.md)

### 기기별 파일 관리

| 파일 | 기기 간 공유 | 설명 |
|------|:-----------:|------|
| `credentials.json` | O | OAuth 클라이언트 (모든 기기 동일) |
| `config.yaml` / `config_blog.yaml` | X | 볼트 경로가 기기마다 다름 |
| `token.json` | X | 기기별 인증 토큰 (자동 생성) |

> 위 파일은 모두 `.gitignore`에 포함되어 있어 git에 올라가지 않습니다.

---

## 데이터 안전성

- **Intent Log (WAL)**: 모든 동기화 액션(업로드/다운로드/삭제)을 실행 **직전에** 디스크에 기록하고 성공 후 resolved 표시. 프로세스가 SIGKILL/블루스크린/전원 차단 등 **어떤 비정상 종료**로 죽어도 재시작 시 미해결 액션이 자동 replay됩니다.
- **로컬 휴지통**: 삭제된 파일은 실삭제가 아닌 `.sync/trash/{uuid}` 경로로 이동됩니다 (기본 30일 보관). `.sync/trash/{uuid}.json` 메타데이터로 원본 위치 추적 가능.
- **Drive 묘비(tombstone)**: Drive에서 삭제된 파일은 `.sync/tombstones/` 폴더로 이동되며 90일간 보관. 모든 활성 기기가 삭제를 확인한 후에만 영구 삭제됩니다.
- **충돌 사본**: 양쪽 동시 편집이 감지되면 **양쪽 내용 모두 보존**됩니다. 패배한 쪽이 `.sync-conflict-{시각}-{기기}.{확장자}` 로 rename.

---

## 충돌 해결 규칙

동일 파일을 두 기기에서 동시 편집한 경우:

| 순서 | 기준 | 동작 |
|---|---|---|
| 1 | Version Vector 비교 | 한쪽이 엄격히 더 크면 → 그 쪽 승리 (나머지는 update 없음) |
| 2 | HLC 카운터 최댓값 | 동률 시 `max(counters.values())` 큰 쪽 승 |
| 3 | Device prefix 비교 | HLC도 동률이면 기기 식별자가 큰 쪽이 **패배** → conflict 사본으로 이름 변경 |

→ 패배한 쪽은 `{원본}.sync-conflict-{YYYYMMDD-HHMMSS}-{기기앞자리}.{확장자}` 로 rename되고 드라이브에도 **일반 파일처럼 동기화**됩니다. 사용자가 두 버전을 비교해 수동 병합 가능.

---

## 문제 해결 (FAQ)

### 앱이 자꾸 죽어요 / 프로세스가 사라졌어요

**원인**: watchdog(파일 감시 C 확장) 또는 SSL 네트워크 불안정 시 드물게 `exit 139` (segfault)로 crash.

**해결**:
- `run_forever.py` 또는 NSSM 서비스로 돌리면 **5초 후 자동 재시작** + Intent Log로 미완 작업 복구
- 수동 실행 중이었다면: `uv run python run_forever.py config.yaml` 으로 전환

### Drive에 같은 파일이 여러 개 생겼어요 (ping-pong 복제)

**원인**: 에코 억제 타이머가 뚫린 경우 발생할 수 있는 알려진 버그 (v2에서 패치됨).

**확인**:
```bash
git log --oneline | grep "prevent upload ping-pong"
```
커밋 `4cb2172` 이상이면 md5 해시 + Drive 재조회 이중 가드 적용됨.

**정리**: Drive 웹에서 중복본 삭제 → 자동 동기화 반영.

### 다른 기기에서 지운 파일이 로컬에 남아있어요

**원인**: Drive 삭제 = `.sync/tombstones/` 로 논리 이동. 로컬 측 poller가 감지해야 반영됩니다.

**해결**: 
- `poll_interval_seconds` (기본 60초) 기다리기
- 로그에 `delete_local: <파일명>` 찍히면 정상 처리 중

### Windows 서비스 등록 시 "로그온 실패"

**원인**: Microsoft 계정 + Windows Hello PIN 환경에서는 PIN이 서비스 로그온에 쓰일 수 없음.

**해결**: LocalSystem으로 전환:
```
fix_service_localsystem.bat 우클릭 → 관리자 권한으로 실행
```

### "사용자가 안 지웠는데 파일이 사라졌어요"

**원인**: PC에 **Google Drive for Desktop** 앱이 설치돼 있으면, 중복 파일을 자동으로 Drive 휴지통(trashed=true)으로 이동시키는 정리 기능이 돕니다. 사용자 몰래 발생 가능.

**확인**: Drive 웹 좌측 "휴지통"에서 해당 파일 조회 → 복원 가능. 

### OAuth 토큰이 만료됐어요 (수개월~1년 후)

**원인**: refresh_token도 오래 안 쓰면 Google이 무효화.

**해결**:
1. 서비스 잠시 중지:
   ```
   nssm stop ObsidianSync
   nssm stop ObsidianSyncBlog
   ```
2. 수동 재인증 (브라우저 열림):
   ```bash
   uv run python -m src.main --config config.yaml
   ```
3. 인증 끝나면 Ctrl+C 종료 후 서비스 재시작:
   ```
   nssm start ObsidianSync
   nssm start ObsidianSyncBlog
   ```

### 첫 동기화가 너무 오래 걸려요

대용량 볼트(5,000~10,000 파일)는 cold start에 **15분~1시간** 소요. 중단하지 말고 기다리세요. `state` 파일이 생긴 이후 재시작은 수 초 내 완료.

---

## 프로젝트 구조

```
obsidian_sync/
├── src/
│   ├── config.py                 # 설정 로드, 제외 패턴, 폴링 상수
│   ├── state.py                  # sync_state.json v2 (Version Vector + 마이그레이션)
│   ├── drive_client.py           # Google Drive API 래퍼 (appProperties + tombstone move)
│   ├── version_vector.py         # HLC 기반 Version Vector (compare/merge/trim)
│   ├── drive_vv_codec.py         # VersionVector ↔ Drive appProperties 인코딩
│   ├── reconciler.py             # 3-way 판정 엔진 (decide/resolve_conflict)
│   ├── sync_engine.py            # 실행 엔진 (upload/download/delete/conflict)
│   ├── trash.py                  # 로컬 .sync/trash/ flat UUID 보관
│   ├── convergence.py            # tombstone 안전 GC 합의 프로토콜
│   ├── intent_log.py             # 부분 실패 복구용 WAL
│   ├── hash.py                   # 청크 md5 (내용 매칭)
│   ├── conflict.py               # .sync-conflict-* 사본 생성
│   ├── local_watcher.py          # watchdog 이벤트 → delete+create 분해
│   ├── poller.py                 # Drive Changes API 적응형 폴링
│   └── main.py                   # 진입점 + AppContext 조립 + 종료 시퀀스
├── run_forever.py                # 크래시 시 5초 후 자동 재시작 래퍼
├── install_service.bat           # NSSM Windows 서비스 원클릭 등록 (관리자)
├── uninstall_service.bat         # 서비스 제거 (관리자)
├── fix_service_localsystem.bat   # 로그온 실패 시 LocalSystem 전환 (관리자)
├── tests/                        # 458 tests
├── docs/
│   ├── 핵심기술.md                # 차별화 기술 14가지 정리 (포트폴리오용)
│   ├── architecture/sync-design.md  # v2 설계 전체 명세
│   ├── journal.md                # 엔지니어링 저널 (시행착오 기록)
│   └── new-device-setup.md       # 새 기기 추가 절차
├── templates/                    # Planner용 스펙 템플릿 (harness 참조)
│   └── gdrive-watchdog-sync.md   # 양방향 sync 시행착오 방지 가이드 (25가지 함정)
├── artifacts/                    # 스프린트 계약·진행·QA 아카이브
├── config.example.yaml           # 설정 템플릿
├── CHANGELOG.md                  # 버전별 변경 이력
├── SETUP_GUIDE.md                # 새 기기 설정 상세 가이드
└── README.md
```

### 동기화에서 제외되는 파일

| 패턴 | 이유 |
|------|------|
| `.obsidian/` | Obsidian 내부 설정 (열 때마다 바뀌어서 충돌 폭탄) |
| `.sync/` | 이 프로그램의 상태 파일, trash, tombstones, intent_log 등 |
| `.trash/` | Obsidian 휴지통 |
| `.smart-env/` | Smart Environment 플러그인 캐시 |
| `.DS_Store` | macOS 시스템 파일 |
| `*.tmp` | 임시 파일 |
| `config.yaml` / `config_blog.yaml` | 사용자별 설정 (기기마다 경로 다름) |
| `credentials.json` / `token.json` | OAuth 비밀 |
| `*.log`, `*.log.*` | 운영 로그 |

---

## 아키텍처 (v2)

v2.0부터 **[Syncthing BEP](https://docs.syncthing.net/specs/bep-v1.html) 스타일의 Version Vector**를 Google Drive `appProperties` 위에 얹어, mtime 의존 없이 **결정적 3-way 동기화**를 수행합니다.

- 파일마다 `{device_prefix → HLC counter}` 벡터가 있어 기기 간 시계 편차에 강함
- 삭제는 실삭제가 아니라 `.sync/tombstones/` 논리 이동 → 다른 기기가 삭제 사실을 놓치지 않음
- 오프라인 기기가 나중에 복귀해도 유령 부활 없음
- 모든 수정(단순 저장 포함)이 vector 증분 이벤트 → 어떤 기기가 언제 수정했는지 보존

### 상세 문서

- [docs/핵심기술.md](docs/핵심기술.md) — 포트폴리오용, 차별화 기술 14가지
- [docs/architecture/sync-design.md](docs/architecture/sync-design.md) — v2 설계 전체 명세, PR 로드맵, 결정 근거
- [docs/journal.md](docs/journal.md) — 엔지니어링 저널 (실제 운영 시행착오 기록)
- [templates/gdrive-watchdog-sync.md](templates/gdrive-watchdog-sync.md) — 다른 프로젝트도 참조하는 "반드시 피할 25가지 함정"
- [CHANGELOG.md](CHANGELOG.md) — 버전별 변경 이력
