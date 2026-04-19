---
template_id: claude-cli-subprocess
domain: Claude Code CLI 자동화/하네스 통합
keywords: [claude, subprocess, headless, automation, cli, harness, permission, agent]
when_to_use: |
  프로젝트에서 `claude` CLI를 비대화(-p) 모드로 서브프로세스 호출하여
  자동화·하네스·파이프라인에 통합할 때.
  커스텀 서브에이전트(.claude/agents/*)를 프로그램적으로 실행하는 경우 포함.
output: artifacts/specs/claude-cli-integration.md
related_templates: [langfuse-setup]
---

# Claude CLI 서브프로세스 통합 템플릿

(레이어 1 — 프로젝트 코드가 `claude` CLI를 외부 프로세스로 호출하는 경우)

## 호출 패턴

```python
import shutil, subprocess
claude = shutil.which("claude") or "claude"
result = subprocess.run(
    [claude, "-p", "--agent", agent_name,
     "--max-turns", str(max_turns),
     "--permission-mode", "acceptEdits",
     prompt],
    cwd=str(project_root),
    capture_output=True, text=True,
    encoding="utf-8", errors="replace",
    timeout=1800,
)
```

## 실전 체크리스트

### 1. 실행 파일 해석 (크로스플랫폼)
- `subprocess.run(["claude", ...])` 리터럴로 넘기면 **Windows에서 실패**
  - npm 전역 설치 시 실제 파일은 `claude.cmd`(배치 래퍼)
  - Python `CreateProcess`는 PATHEXT 자동 탐색을 하지 않음
- 해결: `shutil.which("claude")`로 실제 경로 해석
- macOS/Linux에서도 동일 코드로 동작 (절대경로 반환)

### 2. 권한 모드 (파일 쓰기 필수)
- `-p`(비대화) 모드는 응답자가 없어 Write/Edit 권한 프롬프트가 **자동 거부**됨
- 에이전트가 파일을 생성해야 하면 `--permission-mode acceptEdits` 명시
- 다른 옵션
  - `default` — 프롬프트(비대화 모드에서 블록)
  - `acceptEdits` — 파일 편집 자동 승인 **(권장)**
  - `bypassPermissions` — 전부 승인 (위험, 샌드박스 전용)
  - `plan` — plan 모드

### 3. 에이전트 정의 (tools 필드)
- `.claude/agents/<name>/AGENT.md` frontmatter의 `tools` = 해당 에이전트에 허용된 도구 목록
- 파일 생성이 임무인 에이전트는 반드시 `Write, Edit` 포함
  ```yaml
  ---
  name: planner
  model: opus
  tools: Read, Glob, Grep, Write, Edit
  ---
  ```
- `--permission-mode`와 `tools` 둘 다 통과해야 실제 동작 — 한쪽만 있으면 조용히 실패

### 4. 토큰 추적
- `claude -p` stdout에는 토큰 집계 문구가 표준화되어 있지 않음 → 정규식 파싱 불안정
- 대화형 세션은 `~/.claude/projects/<project-id>/*.jsonl`의 `message.usage` 레코드에서 추출 가능
- **project_id 인코딩 규칙** (cwd → 폴더명): `:`, `\`, `/`, `.`, `_` 전부 `-`로 치환 + 소문자화
  ```python
  cwd = str(Path.cwd().resolve()).lower()
  project_id = re.sub(r"[:\\/._]", "-", cwd)
  session_dir = Path.home() / ".claude" / "projects" / project_id
  ```
- Windows 예: `C:\01.project\obsidian_sync` → `c--01-project-obsidian-sync`

### 5. 타임아웃
- `subprocess.run(..., timeout=N)`은 반드시 지정 (claude가 멈추면 부모도 영구 블록)
- 대규모 스펙/리뷰는 15분 이상 걸리기도 함 → 기본 1800초(30분) 권장
- `subprocess.TimeoutExpired` 캐치해서 부분 결과 복구 경로 준비

### 6. 출력 캡처와 인코딩
- `capture_output=True, text=True, encoding="utf-8", errors="replace"` 묶어서 사용
- 한국어 등 멀티바이트 출력이 `cp949`로 디코딩되면 깨짐 (Windows 기본)
- stderr도 같이 캡처해서 로그 저장 — claude가 경고/에러를 stderr로 보냄

### 7. 작업 디렉토리 (cwd)
- `cwd=str(project_root)` 고정 — 에이전트가 상대경로로 파일을 쓸 때 기준이 됨
- `.claude/agents/`, `.claude/settings.json`도 cwd 기준으로 탐색됨

### 8. 인터랙티브 vs `-p` 모드 분리
- **인터랙티브** (`claude` 단독): 사용자가 직접 권한 응답, stdin/stdout 상속
  - `subprocess.run([claude], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)`
- **비대화** (`claude -p`): 프롬프트/응답 없이 한 번에, 출력은 캡처
- 두 모드는 같은 `claude` 바이너리지만 권한·토큰 추적·타임아웃 전략이 다르므로 함수를 분리할 것

## 진단 순서 (파일이 안 만들어질 때)

1. `claude --version`으로 CLI 존재 확인
2. `.claude/agents/<name>/AGENT.md`의 `tools`에 Write/Edit 있는지
3. 호출 명령에 `--permission-mode acceptEdits` 있는지
4. stdout/stderr 전문을 로그로 저장해 실제 에러 메시지 확인
5. cwd가 의도한 프로젝트 루트인지 (상대경로 파일 쓰기는 cwd 기준)

## 검증
- 비대화 모드로 간단한 "artifacts/test.md를 작성하라" 요청 → 파일 실제 생성되는가
- 에이전트 `tools`에서 Write 제거 시 → 생성 실패(예상)
- `--permission-mode` 제거 시 → 생성 실패(예상)
- Windows/macOS/Linux 모두에서 `shutil.which` 해석 후 성공하는가
