# 코드 검수 보고서: Obsidian ↔ Google Drive 동기화 데몬

> 검수 기준: `지침1.md` | 최초 검수: 2026-04-13 | 개선 후 재검수: 2026-04-13

---

## 항목별 점수표

### 1. 동기화 메커니즘 — **30 / 30** _(이전: 27/30)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| 로컬→Drive: watchdog 이벤트 기반 감지 | 10/10 | `DebouncedHandler` (watcher.py:18) — `on_created/modified/deleted/moved` 콜백 완전 구현 |
| 로컬→Drive: 변경된 파일만 업로드 | 10/10 | 디바운스 후 `upload_file` 호출 + `drive_mtime > local_mtime`이면 업로드 스킵 (drive_sync.py:238) |
| Drive→로컬: Changes API 1분 폴링 | 5/5 | `_poll_loop` (drive_sync.py:367), `poll_interval_seconds: 60` (config.example.yaml:17) |
| 충돌 해결: last-write-wins | 5/5 | ✅ **개선됨** — `_apply_remote_delete()` (drive_sync.py:465) 구현. `sync.delete_local` 옵션(기본 `false`)으로 제어. root check를 trashed 체크 앞으로 이동해 로직 순서도 개선 (drive_sync.py:429) |

---

### 2. 프로젝트 구조 — **10 / 10** _(유지)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| 파일 구조 완전 일치 | 10/10 | 지침 명시 파일 전부 존재. README.md 추가로 완성도 향상 |

---

### 3. 훅 시스템 — **20 / 20** _(이전: 17/20)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| ChangeEvent 데이터클래스 | 5/5 | sync_hook.py:11 — 변경 없음, 그대로 완전 |
| BaseHook 추상 클래스 | 5/5 | sync_hook.py:24 — 변경 없음 |
| SyncHook (1단계) 구현 | 5/5 | 변경 없음 |
| load_hooks 레지스트리 + on_shutdown | 5/5 | ✅ **개선됨** — `self._all_hooks: list[BaseHook] = []` (watcher.py:134)로 훅 추적, `setup()`에서 `self._all_hooks.extend(hooks)` (watcher.py:151), `stop()`에서 순회 호출 + 예외 격리 (watcher.py:171-178) |

---

### 4. 데몬 배포 — **10 / 10** _(이전: 4/10)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| Windows 배포 | 5/5 | ✅ **개선됨** — README.md:125-150: 작업 스케줄러 (로그온 무관, 재시작 설정 포함) + NSSM 서비스 등록 두 가지 방식 모두 상세 가이드 |
| Linux/macOS systemd | 3/3 | ✅ **개선됨** — README.md:152-178: `obsidian-sync.service` 유닛 파일 예시 + `systemctl enable/start` + `journalctl` 로그 확인 |
| pythonw 코드레벨 처리 | 2/2 | main.py:61 — `sys.stderr.fileno()` 예외 처리 그대로 유지 |

---

### 5. 다른 기기 적용 — **5 / 5** _(이전: 3/5)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| config.example.yaml 제공 | 2/2 | Phase 2/3 주석 + `delete_local` 옵션 문서화 추가 (config.example.yaml:18) |
| OAuth 토큰 기기별 생성 | 1/1 | 변경 없음 |
| git clone 이후 설정 가이드 | 2/2 | ✅ **개선됨** — README.md:182-195: `git clone → uv sync → config.yaml 편집 → credentials.json 복사 → uv run python main.py` 완전한 신규 기기 온보딩 플로우 |

---

### 6. 제약사항 — **15 / 15** _(이전: 14/15)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| config.yaml + 토큰 파일 gitignore | 5/5 | 변경 없음 |
| 순환 트리거 방지 | 5/5 | `_apply_remote_delete`에서도 동일 패턴 적용 (drive_sync.py:479) — 일관성 유지 |
| 디바운싱 기본값 fallback | 5/5 | ✅ **개선됨** — `self._config.get("sync", {}).get("debounce_seconds", 5)` (watcher.py:142) — config 키 누락 시 KeyError 없이 기본 5초 적용 |

---

### 7. 기술 스택 — **10 / 10** _(이전: 8/10)_

| 세부 항목 | 점수 | 근거 |
|-----------|------|------|
| Python 3.12 | 2/2 | pyproject.toml:5 `requires-python = ">=3.12"` |
| watchdog | 2/2 | pyproject.toml:11 `watchdog>=6.0.0` |
| google-api-python-client | 2/2 | pyproject.toml:7 |
| PyYAML | 2/2 | pyproject.toml:10 |
| uv 패키지 관리 | 2/2 | ✅ **개선됨** — `.python-version`이 .gitignore에서 제거 (.gitignore:20 해당 줄 삭제) — 기기 간 Python 버전 공유 가능 |

---

### 8. 코드 품질 (보너스 평가) — **5 / 5** _(이전: 4/5)_

| 항목 | 평가 |
|------|------|
| pyproject.toml description | ✅ **개선됨** — `"Bidirectional sync daemon between local Obsidian vault and Google Drive"` (pyproject.toml:4) |
| `_apply_remote_delete` 순환 방지 일관성 | ✅ 다운로드와 동일한 ignore_paths 패턴 적용 |
| `_apply_drive_change` 로직 순서 개선 | ✅ root check → trashed check 순서로 변경 (불필요한 삭제 처리 방지) |
| 스레드 안전성 | ✅ 그대로 유지 |
| 나머지 기존 품질 항목 | ✅ 전부 유지 |

---

## 총점 요약

| 카테고리 | 배점 | 이전 | 개선 후 | 변화 |
|----------|------|------|---------|------|
| 동기화 메커니즘 | 30 | 27 | **30** | +3 |
| 프로젝트 구조 | 10 | 10 | **10** | — |
| 훅 시스템 | 20 | 17 | **20** | +3 |
| 데몬 배포 | 10 | 4 | **10** | +6 |
| 다른 기기 적용 | 5 | 3 | **5** | +2 |
| 제약사항 | 15 | 14 | **15** | +1 |
| 기술 스택 | 10 | 8 | **10** | +2 |
| 코드 품질 (보너스) | 5 | 4 | **5** | +1 |
| **합계** | **105** | **87** | **105** | **+18** |

---

## 결함 처리 현황

| 우선순위 | 결함 | 상태 |
|----------|------|------|
| HIGH | `VaultWatcher.stop()`에서 `on_shutdown()` 미호출 | ✅ 해결 |
| HIGH | README.md 부재 | ✅ 해결 |
| MEDIUM | Drive 삭제 시 로컬 미반영 | ✅ 해결 (`delete_local` 옵션) |
| MEDIUM | `debounce_seconds` 없으면 KeyError | ✅ 해결 |
| LOW | `.python-version` gitignore 포함 | ✅ 해결 |
| LOW | pyproject.toml description 기본값 | ✅ 해결 |
| LOW | config.example.yaml `delete_local` 미문서화 | ✅ 해결 |

**잔존 관찰 사항 (영향도 낮음):**
- main.py:76 — `watch_paths[0]`만 DriveSync의 `vault_root`로 사용. `VaultWatcher`는 다중 경로를 지원하나 `DriveSync`는 단일 vault_root 가정. 다중 볼트 지원 시 재설계 필요.

---

## 결론

보고서에 명시된 7개 결함이 **모두 수정**되었다. 특히 `_apply_remote_delete` 구현은 `ignore_paths` 순환 방지 패턴을 다운로드와 동일하게 적용하여 일관성도 높다. **105/105 만점** 달성.
