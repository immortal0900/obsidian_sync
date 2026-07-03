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

→ 패배한 쪽은 `{원본}.sync-conflict-{YYYYMMDD-HHMMSS}-{기기앞자리}.{확장자}` 로 보존되고 드라이브에도 **일반 파일처럼 동기화**됩니다. 사용자가 두 버전을 비교해 수동 병합 가능.

---

## 동기화가 어긋나지 않는 원리 (상세)

이 프로그램의 목표는 딱 두 가지입니다: **① 로컬과 드라이브의 내용이 서로 어긋나지 않게 하고, ② 지운 파일이 다시 살아나지 않게** 하는 것. 아래는 그 두 가지가 어떻게 보장되는지를 코드 동작 그대로 풀어 쓴 것입니다.

### 0. 전체 구조 — 4개의 부품이 한 줄로 협력한다

동기화는 네 부품이 **순서대로** 일합니다. 감지 → 판정 → 실행 → 기록.

| 부품 | 파일 | 하는 일 |
|------|------|---------|
| **감지기 2개** | `local_watcher.py` (로컬) · `poller.py` (드라이브) | 어느 쪽에서 무엇이 바뀌었는지 **알아챈다** |
| **판정 엔진** | `reconciler.py` | 로컬 상태 vs 원격 상태를 비교해 **무슨 작업을 할지 결정한다** (업로드/다운로드/삭제/충돌) |
| **실행 엔진** | `sync_engine.py` | 결정된 작업을 **실제로 수행하고** 안전장치를 건다 |
| **상태 파일** | `state.py` → `.sync/sync_state.json` | "마지막으로 맞춰진 상태"를 **기준선(baseline)으로 기록한다** |

```
[로컬 편집]                              [드라이브 편집]
   │                                        │
watcher(감지)                           poller(감지, 10~120초 적응)
   │                                        │
   └──────────► sync_engine (단일 잠금) ◄────┘   ← 한 번에 하나씩만 실행 (경쟁 방지)
                     │
                reconciler.decide(로컬, 원격)   ← "수정 시각"이 아니라 "Version Vector"로 판정
                     │
        upload / download / delete_remote / delete_local / conflict
                     │
                .sync/sync_state.json 갱신 (원자적 저장)
```

두 감지기가 동시에 이벤트를 던져도 실행 엔진은 **단일 잠금(single-flight lock, 한 번에 한 작업만)** 으로 직렬 처리합니다. 잠금 보유 중 들어온 작업은 내부 큐에 쌓였다가 순서대로 처리되므로, "로컬 업로드와 원격 다운로드가 뒤엉켜 서로를 덮어쓰는" 사고가 원천적으로 안 생깁니다.

> 참고: 로컬 감지는 디바운스(debounce, 연속 저장 이벤트를 `debounce_seconds` 동안 묶어 1회로 전달)를 거치지만 **삭제 이벤트는 디바운스 없이 즉시** 전파합니다. 원격 감지 간격은 코드가 활동량에 따라 **10초~120초 사이에서 자동 조절**합니다(고정 설정이 아님 — `config.yaml`의 `poll_interval_seconds`는 현재 코드가 읽지 않는 잔재 키입니다).

---

### 1. 판정 기준은 "수정 시각"이 아니라 "Version Vector"다

가장 흔한 동기화 버그는 **파일 수정 시각(mtime)** 으로 신구를 판단하는 것입니다. 기기마다 시계가 조금씩 다르고(시계 편차), 클라우드가 메타데이터만 건드려도 시각이 바뀌기 때문에 mtime은 신뢰할 수 없습니다.

그래서 이 프로그램은 파일마다 **Version Vector(버전 벡터, "어느 기기가 이 파일을 몇 번 바꿨는지" 적어둔 작은 표)** 를 붙여 인과관계로 판정합니다. Syncthing(오픈소스 동기화 도구)의 BEP 명세를 포팅했습니다.

```
note.md 의 버전 벡터 예시:
   { "a3b4c5d6": 1745000000123,   ← 기기 A가 마지막으로 찍은 값
     "b1c2d3e4": 1745000050456 }  ← 기기 B가 마지막으로 찍은 값
```

- **키**: 기기 식별자(device_id)의 앞 8자. device_id는 `config.yaml`의 `device_id`, 없으면 컴퓨터 호스트명입니다.
- **값**: HLC(Hybrid Logical Clock, 하이브리드 논리 시계). 파일을 건드릴 때마다 `max(기존값 + 1, 현재_unix_ms)` 로 올립니다. → **시계가 거꾸로 가도 값은 반드시 증가**하므로 시계 편차에 강하고, 전체 순서(total order)가 강제됩니다.
- **불변식**: 쓰기성 이벤트(생성/수정/삭제/rename)마다 예외 없이 `version.update(내 기기)` 를 호출합니다. "단순 저장(내용은 그대로, 시각만 바뀜)"도 벡터를 올립니다.

두 벡터를 `compare()` 하면 네 가지 결과가 나오고, 그에 따라 작업이 결정됩니다:

| 비교 결과 | 뜻 | 동작 |
|-----------|-----|------|
| **Equal** | 두 쪽 이력이 완전히 같음 | 아무것도 안 함 |
| **Greater** | 로컬이 원격 이력을 모두 포함 + 더 나아감 | **업로드** (로컬이 삭제 상태면 → 원격도 삭제) |
| **Lesser** | 원격이 로컬 이력을 모두 포함 + 더 나아감 | **다운로드** (원격이 삭제 상태면 → 로컬도 삭제) |
| **Concurrent** | 양쪽이 서로 모르는 수정을 각자 함 | **충돌 처리** (양쪽 내용 모두 보존, §9) |

이 표가 "어긋나지 않음"의 뼈대입니다. mtime 추측이 아니라 **누가 먼저이고 누가 나중인지, 아니면 진짜 동시인지**를 수학적으로 확정하기 때문에, 같은 입력이면 어느 기기에서 돌려도 같은 결론(결정적, deterministic)이 나옵니다.

드라이브 쪽에는 이 벡터를 파일의 **appProperties(앱 전용 커스텀 속성, 파일당 30개 key 제한)** 에 `ot_sync_vv_<기기앞자리>=<HLC값>` 형태로 얹어 저장합니다(`drive_vv_codec.py`). 기기가 28대를 넘으면 값이 큰 상위 28개만 남기고 잘라냅니다(`trim`).

---

### 2. 같은 내용은 절대 다시 전송하지 않는다 — 내용 해시 단락

양쪽에 파일이 다 있을 때, 판정 엔진은 **벡터를 비교하기 전에 먼저 내용 지문을 봅니다**:

```
로컬 md5 == 원격 md5  그리고  로컬 size == 원격 size  →  전송 생략, 벡터만 병합(merge)
```

md5(파일 내용을 128비트로 압축한 지문)와 크기가 같으면 내용이 동일하다는 뜻이므로, 업로드도 다운로드도 하지 않고 두 벡터의 이력만 합칩니다(`UpdateVectorOnly`). 이것이 없으면 "같은 파일을 서로 최신이라 우기며 무한히 주고받는" 핑퐁이 생깁니다. 100MB를 넘는 파일은 해시 계산을 건너뛰고(`None`) 벡터 비교로만 판정합니다.

---

### 3. "방금 편집한 내용"을 되돌리지 않는다 — 3-way md5 다운로드 가드

실전에서 가장 위험한 순간은 이겁니다: 내가 파일을 올린 직후, 그 사이에 내가 **한 번 더 편집**했는데, 드라이브가 "파일 바뀌었어요"라고 뒤늦게 알려와서 **내 새 편집을 방금 올린 옛 버전으로 덮어쓰는** 경우. `sync_engine`은 다운로드 직전에 세 개의 md5를 비교해 이를 막습니다.

- **L** = 지금 로컬 파일의 실제 md5
- **S** = 상태 파일에 기록된 md5 (= 마지막으로 맞춰졌던 값)
- **D** = 드라이브의 현재 md5

| 상황 | 의미 | 동작 |
|------|------|------|
| **L = S = D** | 셋 다 같음 | 다운로드 생략 |
| **L = S, S ≠ D** | 드라이브만 바뀜 (진짜 원격 변경) | **정상 다운로드** |
| **L ≠ S, L = D** | 로컬이 이미 드라이브와 같음 (상태 기록만 낡음) | 다운로드 생략, 상태만 갱신 |
| **L ≠ S, L ≠ D, D = S** | 우리가 올린 뒤 사용자가 또 편집함 | **다운로드 생략 → 편집 보호**. 다음 주기에 새 편집을 업로드 |
| **L ≠ S, L ≠ D, D ≠ S** | 양쪽이 서로 다르게 편집 (진짜 동시편집) | **충돌 사본 생성 후** 다운로드 |

네 번째 줄이 핵심 안전장치입니다("빠른 편집 보호"). "드라이브 = 내가 마지막에 올린 것(D=S)인데 로컬은 그보다 더 나감"이면, 다운로드는 곧 데이터 손실이므로 건너뜁니다.

---

### 4. 내가 쓴 걸 내가 다시 감지하는 메아리를 막는다 — 2중 에코 억제

내가 파일을 다운로드하면 로컬 watcher가 "새 파일이다!"라고 또 감지하고, 내가 업로드하면 poller가 "드라이브가 바뀌었다!"라고 또 감지합니다. 이 **메아리(echo)** 를 그대로 두면 무한 루프가 됩니다. 두 겹으로 막습니다:

1. **시간창 억제 (15초)**: 방금 우리가 쓴 로컬 경로 / 업로드한 drive_id를 15초간 무시 목록에 올려둡니다.
2. **내용 해시 억제 (영속)**: 시간창이 뚫려도, 업로드 직전 `상태 md5 == 로컬 md5`면 건너뛰고(watcher 쪽), 원격 변경의 `md5 == 상태 md5`면 건너뜁니다(poller 쪽). 시간과 무관하게 내용으로 판별하므로 더 튼튼합니다.

또한 상태 파일에 drive_id가 없는데 업로드하려 할 때, 드라이브에 **같은 경로의 파일이 이미 있으면 그 id를 재사용**(`find_file_by_rel_path`)해 중복 파일 폭증을 막습니다.

---

### 5. 지운 파일이 되살아나지 않는 원리 — Tombstone + 벡터 증가 ⭐

여기가 이 프로젝트의 심장입니다. 순진하게 만들면 삭제는 반드시 부활합니다. 이유는: **"내 상태엔 파일이 있는데 반대쪽엔 없네 → 반대쪽이 실수로 지웠나 보다, 다시 올려/내려받자"** 라고 오판하기 때문입니다. 이 프로그램은 세 가지 장치로 이를 막습니다.

**(1) 삭제도 "수정"이다 — 삭제하면 벡터가 커진다**

파일을 지우면 실행 엔진은 그 파일의 버전 벡터를 `update()` 로 **한 칸 올립니다**. 즉 삭제 표식(tombstone)은 삭제 직전의 살아있던 버전보다 **항상 더 큰 벡터**를 갖습니다. 이 사실이 "삭제가 편집을 이긴다"의 근거입니다.

**(2) 실삭제가 아니라 논리 삭제 — 드라이브 파일을 `.sync/tombstones/`로 옮긴다**

드라이브에서 파일을 진짜 삭제하면 거기 얹어둔 appProperties(=버전 벡터)도 함께 사라집니다. 벡터가 사라지면 "얼마나 최신 상태로 지워졌는지" 근거가 없어져 다른 기기가 부활시킬 수 있습니다. 그래서 삭제 시 파일을 **`.sync/tombstones/` 폴더로 이동**시키고 `ot_sync_deleted=1` 을 기록해 **벡터를 살려둡니다**(`move_to_tombstones`). 다른 기기의 poller는 이 폴더 이동을 "삭제"로 감지합니다.

**(3) 부활 시나리오 단계 분해 — 오프라인 기기가 돌아와도 안전**

기기 A, B 두 대가 `note.md`(버전 `{A:5}`)를 공유 중이라고 합시다.

| 단계 | 무슨 일 | 벡터 상태 |
|------|---------|-----------|
| 1 | 기기 **A**에서 `note.md` 삭제 | 드라이브 파일 → `.sync/tombstones/`로 이동, `deleted=1`, 벡터 `{A:5}` → **`{A:6}`** |
| 2 | 기기 **B**는 그동안 꺼져 있었음 | B의 상태엔 `note.md`가 아직 살아있고 벡터는 옛날 **`{A:5}`** |
| 3 | 기기 **B** 재시작 → 폴링으로 tombstone(`deleted=1`, `{A:6}`) 수신 | — |
| 4 | 판정: B의 `{A:5}` vs 원격 `{A:6}` → **Lesser** + 원격이 `deleted` → **DeleteLocal** | B의 `note.md`도 `.sync/trash/`로 이동. **부활 없음** |

핵심은 4단계입니다. B는 "내 상태엔 있는데 드라이브엔 없으니 새로 올리자"라고 **오판하지 않습니다**. tombstone의 벡터 `{A:6}`이 자기 `{A:5}`보다 명백히 크기 때문에, "이건 나중에 일어난 삭제구나"를 확정합니다.

> **예전(고장 났던) 방식과 대비**: 과거엔 원격 삭제를 감지하면 상태에서 drive_id만 지우고 로컬 파일은 남겼습니다. 그 파일이 나중에 수정되면 drive_id 없는 "신규"로 인식돼 **다시 업로드 = 부활**했습니다. 지금은 tombstone 벡터 비교로 이 경로를 원천 차단합니다.

**(4) 삭제 vs 편집이 진짜 겹치면 → 조용히 지우지 않고 충돌 처리**

만약 B가 꺼져 있는 동안 `note.md`를 **편집도 했다면** B의 벡터는 `{A:5, B:3}`처럼 자기 카운터가 올라갑니다. 이건 tombstone `{A:6}`과 서로 포함관계가 아니므로 **Concurrent(동시)** → 충돌 처리됩니다. 즉 "한쪽은 지웠고 한쪽은 고쳤다"는 애매한 상황에서 편집 내용이 소리 없이 사라지지 않고 사본으로 보존됩니다.

**(5) 삭제된 파일은 즉시 완전 삭제되지 않는다 — 복구 가능**

| 방향 | 실제 파일의 행선지 | 보관 |
|------|-------------------|------|
| **로컬에서 삭제** | 드라이브 파일 → `.sync/tombstones/` (로컬 파일은 사용자가 이미 지운 상태) | 90일 |
| **드라이브에서 삭제** | 로컬 파일 → `.sync/trash/{uuid}` + `{uuid}.json` 메타(원본 경로·mtime·md5·삭제시각) | 30일 |

`.sync/trash/`는 Windows 경로 길이 제한(MAX_PATH)을 피하려고 폴더 구조 없이 **평평한 UUID 이름**으로 저장합니다. 보관 기간이 지나면 자동 정리(GC)되지만, 그전까지는 언제든 복구할 수 있습니다.

---

### 6. 모든 기기가 "봤다"고 확인해야 진짜 삭제 — Convergence GC

tombstone이 영원히 쌓이지 않도록 정리하되, **아직 삭제 사실을 못 본 기기가 하나라도 있으면** 정리하지 않습니다(그 기기가 부활시킬 수 있으므로). 두 조건이 **모두** 충족돼야 tombstone을 최종 삭제(휴지통 이동)합니다:

1. **합의(convergence)**: 드라이브의 공유 파일 `.sync/convergence.json`에 모든 활성 기기가 "이 tombstone을 확인했다"고 기록. (동시 수정 경쟁은 Drive `version` 필드를 이용한 낙관적 동시성 제어 + 지수 백오프 재시도로 처리)
2. **보관 기간**: 삭제 후 `tombstone_retention_days`(기본 90일) 경과.

영구히 사라진 기기는 blacklist(제외 목록)에 넣어 나머지 기기끼리 합의가 끝나게 합니다. 이 프로토콜 덕분에 "느린 기기가 나중에 접속해 이미 지운 파일을 되살리는" 최악의 경우가 막힙니다.

---

### 7. 상태 파일이 없거나 깨져도 안전 — `run_without_state`

새 기기 첫 실행이거나 `.sync/sync_state.json`이 손상되면, 기준선이 없어 "삭제 의도"를 "신규"로 오판하기 쉽습니다(예전 부활 버그의 근원). 이때는 **전체 목록 대조 모드**로 전환합니다:

- 드라이브 전체 파일 + `.sync/tombstones/` 내용까지 모두 읽어옵니다.
- 로컬·원격 **양쪽에 있고 md5가 같으면** → 전송 없이 벡터만 병합.
- **tombstone만 있으면** → 로컬 상태에 `deleted=True`로 흡수(파일 작업 없음). → 지운 파일이 되살아나지 않음.
- **양쪽에 있는데 내용이 다르고 로컬 벡터가 비어 있으면**(상태 유실) → 함부로 덮어쓰지 않고 **강제 충돌 처리**로 로컬 편집을 사본으로 보존.

손상된 상태 파일은 `.backup`으로 옮겨 보존하고, v1 스키마는 자동으로 v2로 마이그레이션(백업 후 변환)합니다.

---

### 8. 작업 도중 강제 종료돼도 복구 — Intent Log (WAL)

"드라이브 파일을 tombstone으로 옮겼는데 상태 파일 저장 직전에 전원이 나가면?" 같은 **부분 실패**를 막기 위해, 모든 작업은 실행 **직전에** `.sync/intent_log.jsonl`(WAL, Write-Ahead Log — 선기록 로그)에 기록되고 성공 후 `resolved`로 표시됩니다(각 기록은 `fsync`로 디스크에 강제 반영). 재시작하면 **미해결(resolved가 없는) 작업을 자동 재실행(replay)** 하므로, SIGKILL·블루스크린·전원 차단 어떤 비정상 종료에서도 작업이 유실되지 않습니다. 같은 작업을 다시 실행해도 결과가 같도록(멱등, idempotent) 설계돼 있어 재실행이 안전합니다.

---

### 9. 충돌 = 어느 쪽도 잃지 않는다

`compare()`가 **Concurrent(양쪽이 서로 모르는 수정)** 로 나오면, 실행 엔진(`_do_conflict`)은 로컬 내용을 `{원본}.sync-conflict-{YYYYMMDD-HHMMSS}-{기기앞자리}.{확장자}` 사본으로 **복사해 보존**한 뒤(Syncthing 호환 명명), 원래 경로에는 원격 버전을 내려받습니다. 즉 정본 경로는 원격 버전이 차지하고 로컬 편집은 사본으로 남으므로, **두 버전이 모두 살아남고** 충돌 사본은 일반 파일처럼 다시 전 기기에 동기화됩니다. (위 [충돌 해결 규칙](#충돌-해결-규칙)의 tiebreaker는 어느 쪽이 "패배"인지를 정의하는 규칙이며, 실행 단계에서는 항상 로컬본을 사본으로 보존하는 보수적 방식을 취합니다.) 어느 경우에도 내용이 조용히 소멸하지 않습니다.

---

### 한마디 요약

**"수정 시각 추측"을 버리고 "누가 언제 무엇을 했는지"를 Version Vector로 기록·비교**하기 때문에 로컬과 드라이브가 어긋나지 않고, **삭제도 하나의 수정으로 벡터를 올린 tombstone(논리 삭제)** 으로 남겨 모든 기기가 확인할 때까지 보존하기 때문에 지운 파일이 되살아나지 않습니다. 그 위에 3-way md5 가드·2중 에코 억제·WAL·단일 잠금이 데이터 손실과 경쟁 상태를 막습니다.

> 코드로 더 깊이: [reconciler.py](src/reconciler.py)(판정) · [sync_engine.py](src/sync_engine.py)(실행·가드) · [version_vector.py](src/version_vector.py)(HLC 비교) · [drive_client.py](src/drive_client.py)(tombstone·경로 복원) · [convergence.py](src/convergence.py)(합의 GC) · [docs/architecture/sync-design.md](docs/architecture/sync-design.md)(설계 명세)

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

> 실제 동작 원리(어긋나지 않는 이유, 삭제가 부활하지 않는 이유)는 위 [동기화가 어긋나지 않는 원리 (상세)](#동기화가-어긋나지-않는-원리-상세) 참고.

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
