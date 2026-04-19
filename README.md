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

## 빠른 시작

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
2. OAuth 클라이언트 ID (데스크톱 앱) 생성 → JSON 다운로드 → `credentials.json`으로 저장
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
  delete_local: false        # true면 Drive에서 삭제 시 로컬도 삭제
```

### 4단계: 실행

```bash
uv run python main.py
```

첫 실행 시 브라우저가 열립니다 → Google 로그인 → Drive 접근 허용 → 완료!
이후부터는 브라우저 없이 바로 시작됩니다.

```
=== Obsidian Sync Daemon starting ===
Google Drive service ready
Daemon running. Press Ctrl+C to stop.
```

---

## 다른 기기 추가

이미 한 기기에서 동작 중이고, PC/노트북/서버 등 다른 기기를 추가하려면:

```bash
git clone <repo-url> && cd obsidian_sync && uv sync
cp config.example.yaml config.yaml
# config.yaml 편집 (이 기기의 볼트 경로 + 같은 folder_id)
# credentials.json 복사 (기존 기기에서 가져오거나 Cloud Console에서 재다운로드)
uv run python main.py   # 브라우저 OAuth 인증
```

단계별 상세 가이드: [SETUP_GUIDE.md](SETUP_GUIDE.md)

### 기기별 파일 관리

| 파일 | 기기 간 공유 | 설명 |
|------|:-----------:|------|
| `credentials.json` | O | OAuth 클라이언트 (모든 기기 동일) |
| `config.yaml` | X | 볼트 경로가 기기마다 다름 |
| `token.json` | X | 기기별 인증 토큰 (자동 생성) |

> 위 파일은 모두 `.gitignore`에 포함되어 있어 git에 올라가지 않습니다.

---

## 백그라운드 실행 (선택)

### 콘솔 창 없이 실행

```bash
uv run pythonw main.py
```

### 부팅 시 자동 실행 — Windows (NSSM 서비스)

```cmd
nssm install ObsidianSync
```

| 항목 | 값 |
|------|-----|
| Path | `.venv\Scripts\python.exe` |
| Arguments | `main.py` |
| Startup directory | 프로젝트 폴더 경로 |

```cmd
nssm start ObsidianSync       # 시작
nssm status ObsidianSync      # 상태 확인
nssm restart ObsidianSync     # 재시작
```

### 부팅 시 자동 실행 — Linux/macOS (systemd)

`/etc/systemd/system/obsidian-sync.service`:

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

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable obsidian-sync && systemctl start obsidian-sync
```

---

## 로그 확인

```bash
tail -f obsidian_sync.log          # Linux/macOS
type obsidian_sync.log             # Windows
```

로그 파일은 5MB마다 교체되며 최대 3개 보관됩니다.

---

## 테스트

```bash
uv sync --extra dev                  # 개발 의존성 설치 (pytest, ruff)
uv run python -m pytest tests/ -v    # 458 tests 실행
uv run ruff check src/ tests/        # 정적 분석
```

현재 기준: **458 passed, 2 skipped, 0 failures** + `ruff check` 통과.

---

## 아키텍처 (v2)

v2.0부터 **[Syncthing BEP](https://docs.syncthing.net/specs/bep-v1.html) 스타일의 Version Vector**를 Google Drive `appProperties` 위에 얹어, mtime 의존 없이 **결정적 3-way 동기화**를 수행합니다.

- 파일마다 `{device_prefix → HLC counter}` 벡터가 있어 기기 간 시계 편차에 강함
- 삭제는 실삭제가 아니라 `.sync/tombstones/` 논리 이동 → 다른 기기가 삭제 사실을 놓치지 않음
- 오프라인 기기가 나중에 복귀해도 유령 부활 없음
- 모든 수정(단순 저장 포함)이 vector 증분 이벤트 → 어떤 기기가 언제 수정했는지 보존

상세 설계, PR 로드맵, 결정 근거는 [docs/architecture/sync-design.md](docs/architecture/sync-design.md) 참조.

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
├── tests/                        # 458 tests
├── docs/
│   ├── architecture/sync-design.md  # v2 설계 전체 명세
│   ├── journal.md                   # 엔지니어링 저널
│   └── new-device-setup.md          # 새 기기 추가 절차
├── artifacts/                    # 스프린트 계약·진행·QA 아카이브
├── config.example.yaml           # 설정 템플릿
├── SETUP_GUIDE.md                # 새 기기 설정 상세 가이드
└── README.md
```

### 동기화에서 제외되는 파일

| 패턴 | 이유 |
|------|------|
| `.obsidian/` | Obsidian 내부 설정 (열 때마다 바뀌어서 충돌 폭탄) |
| `.sync/` | 이 프로그램의 상태 파일 |
| `.trash/` | Obsidian 휴지통 |
| `.smart-env/` | Smart Environment 플러그인 캐시 |
| `.DS_Store` | macOS 시스템 파일 |
| `*.tmp` | 임시 파일 |

> `.obsidian/` 제외는 1차 구현 결정이며, 향후 `.obsidian/plugins/` 등 선택적 동기화 옵션을 추가할 예정입니다.
