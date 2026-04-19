# obsidian_sync — Version Vector 기반 동기화 재설계

**문서 목적**: 이 문서는 독립 세션의 개발자가 PR1~PR4를 순차적으로 구현할 수 있도록 작성된 설계 명세다. 각 PR은 선행 PR을 전제로 하되, 단독 리뷰·병합 가능한 단위로 분할됐다.

**상태**: 설계 완료 / 구현 전.

---

## 1. 배경과 현재 구현의 결함

### 1.1 보고된 증상

사용자가 현재 `obsidian_sync`에서 보고한 4가지 양방향 동기화 증상:

1. 구글드라이브에 파일 추가 → 로컬에 없으면 **지워짐** (기대: download)
2. 로컬에 파일 추가 → 구글드라이브에 없으면 **지워짐** (기대: upload)
3. 로컬에서 지워도 구글드라이브에서 **다시 내려받음**
4. 구글드라이브에서 지워도 로컬에서 **다시 올라감**

증상 3·4는 즉각 재현되며, 증상 1·2는 재설치 / 상태 파일 손상 상황에서 재현된다.

### 1.2 근본 원인 (현재 코드 기준)

현재 `sync_state.json`의 `files` dict가 baseline(B)으로 쓰이는 3-way 분류 구조([src/reconciler.py](../../src/reconciler.py)의 16셀 규칙)이나 3가지 허점이 존재한다.

**허점 1 — `REMOTE_DELETED + LOCAL_UNCHANGED` 비대칭**  
[src/reconciler.py:294-298](../../src/reconciler.py#L294-L298):
```python
if remote_kind == REMOTE_DELETED:
    self._state.remove_file(path)   # drive_id만 제거
    return None                      # 로컬 파일은 보존 (Policy 1)
```
→ 로컬 파일이 나중에 수정되면 drive_id 없는 신규로 재업로드 → **증상 4**.

**허점 2 — `run_without_state()`는 baseline 없이 mtime만 봄**  
[src/reconciler.py:74-148](../../src/reconciler.py#L74-L148). 새 기기 첫 동기화·상태 파일 손상 시 삭제 의도를 모두 "신규"로 오판 → **증상 3·4의 근본 원인**.

**허점 3 — Tombstone 부재**  
오프라인 기기가 재접속하면 자기 state에 남은 파일을 "신규"로 인식해 부활시킴.

### 1.3 설계 목표

- 사용자 요구: "공수가 많이 들어도 근본적 해결책 + 파일 단순 수정도 반영 + 공식 문서 기반".
- 해결 전략: **Syncthing BEP의 Version Vector**를 Google Drive `appProperties` 위에 얹어 **Hybrid Logical Clock** 기반 결정적 3-way 동기화를 구현한다.

---

## 2. 공식 문서 기반 설계 근거

이 설계의 모든 핵심 결정은 **공식 명세**를 근거로 한다.

### 2.1 Syncthing Block Exchange Protocol (BEP)

| 출처 | 인용 |
|---|---|
| [BEP v1 Spec](https://docs.syncthing.net/specs/bep-v1.html) | `Vector` = repeated `Counter`, `Counter = {id: uint64, value: uint64}`. id = 기기 ID의 첫 64비트. |
| 동 위 | `deleted=true` + `blocks=[]` + `modified_time=삭제시각` 은 **tombstone** (논리 삭제). |
| 동 위 | "Folder + Name + Version의 조합이 파일 내용을 고유하게 식별". |
| [lib/protocol/vector.go (소스)](https://raw.githubusercontent.com/syncthing/syncthing/main/lib/protocol/vector.go) | `Update(id)`: **`value = max(existing_value + 1, current_unix_timestamp)`** → HLC. |
| 동 위 | `Compare` 반환: `Equal` / `Greater` / `Lesser` / `ConcurrentGreater` / `ConcurrentLesser`. |
| [PR #10207](https://github.com/syncthing/syncthing/pull/10207) | "deleted always loses" 설계가 total order를 깨서 폐기됨 → 삭제도 일반 버전 취급. **우리 Policy 1 제거 근거**. |
| [Forum: conflict resolution](https://forum.syncthing.net/t/how-does-conflict-resolution-work/15113) | Concurrent 시 tiebreaker — 수정 시간 → 동률이면 device ID 큰 쪽 패배 → `.sync-conflict-<date>-<time>-<modifiedBy>.<ext>` 로 rename. |

### 2.2 Google Drive API v3

| 출처 | 인용 |
|---|---|
| [Properties & appProperties](https://developers.google.com/workspace/drive/api/guides/properties) | `appProperties`: 앱별 최대 **30개** key/value. 공개 `properties`와 합쳐 100개까지. |
| 동 위 | 각 쌍의 **key+value 합쳐 최대 124바이트 UTF-8**. |
| 동 위 | `files.update` PATCH로 추가/수정. `null` 설정으로 삭제. 검색 쿼리 지원. |
| [files 리소스 참조](https://developers.google.com/workspace/drive/api/reference/rest/v3/files) | `parents` 필드로 폴더 이동 가능 (단일 parent 기준). |
| [changes 리소스 참조](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes) | Changes API에서 `parents` 변경도 하나의 change로 감지됨. |

### 2.3 설계 함의

- **기기별 key 분리**: `ot_sync_vv_<device_prefix>` 형태로 key를 쪼개면 30 기기까지 수용.
- **appProperties는 파일 삭제와 함께 소실**되므로, 삭제 시 실삭제가 아닌 **`.sync/tombstones/` 폴더 move**를 사용해 vector를 보존.
- HLC 덕분에 **로컬 시계 편차 방어**와 **total order 강제**를 동시에 달성.

---

## 3. 아키텍처

### 3.1 핵심 데이터 모델

```python
# src/version_vector.py (신규 PR1)
from dataclasses import dataclass
from enum import Enum
import time


class VectorOrdering(Enum):
    Equal = "equal"
    Greater = "greater"
    Lesser = "lesser"
    ConcurrentGreater = "concurrent_greater"
    ConcurrentLesser = "concurrent_lesser"


@dataclass(frozen=True)
class VersionVector:
    """Syncthing BEP lib/protocol/vector.go 포팅. HLC 기반."""
    counters: dict[str, int]   # device_id_prefix(8자 hex) -> HLC ms

    @staticmethod
    def empty() -> "VersionVector":
        return VersionVector({})

    def update(self, device_id: str, now: float | None = None) -> "VersionVector":
        """HLC 갱신: max(existing+1, unix_ms).
        Syncthing lib/protocol/vector.go의 Update 로직 포팅.
        """
        ts_ms = int((now if now is not None else time.time()) * 1000)
        prefix = device_id[:8]
        existing = self.counters.get(prefix, 0)
        new_value = max(existing + 1, ts_ms)
        return VersionVector({**self.counters, prefix: new_value})

    def compare(self, other: "VersionVector") -> VectorOrdering:
        keys = set(self.counters) | set(other.counters)
        a_ge = all(self.counters.get(k, 0) >= other.counters.get(k, 0) for k in keys)
        b_ge = all(other.counters.get(k, 0) >= self.counters.get(k, 0) for k in keys)
        if a_ge and b_ge:
            return VectorOrdering.Equal
        if a_ge:
            return VectorOrdering.Greater
        if b_ge:
            return VectorOrdering.Lesser
        sa, sb = sum(self.counters.values()), sum(other.counters.values())
        return (VectorOrdering.ConcurrentGreater if sa >= sb
                else VectorOrdering.ConcurrentLesser)

    def merge(self, other: "VersionVector") -> "VersionVector":
        """union of max — 두 vector의 element-wise max."""
        keys = set(self.counters) | set(other.counters)
        return VersionVector({k: max(self.counters.get(k, 0), other.counters.get(k, 0))
                              for k in keys})

    def trim(self, max_devices: int = 28) -> "VersionVector":
        """appProperties 30-key 제한 대응. 예비 2슬롯(schema, deleted) 확보."""
        if len(self.counters) <= max_devices:
            return self
        kept = sorted(self.counters.items(), key=lambda kv: kv[1], reverse=True)[:max_devices]
        return VersionVector(dict(kept))
```

```python
# src/state.py FileEntry 확장 (PR1)
@dataclass
class FileEntry:
    mtime: float
    size: int
    md5: str | None = None                           # PR3에서 채움
    drive_id: str | None = None
    version: VersionVector = field(default_factory=VersionVector.empty)
    deleted: bool = False
    deleted_at: float | None = None                  # tiebreaker용
```

### 3.2 모든 이벤트의 Vector 증분 규칙

**핵심 불변식**: 쓰기성 이벤트마다 `version.update(self.device_id)` 호출. "단순 수정(mtime만 바뀜)"도 예외 없음.

| 이벤트 | 트리거 | Vector 동작 |
|---|---|---|
| 로컬 생성 | watchdog `on_created` | `version = empty.update(self.device_id)` |
| 로컬 수정 | watchdog `on_modified` | `version = current.update(self.device_id)` |
| 로컬 삭제 | watchdog `on_deleted` | `version = current.update(self.device_id)` + `deleted=True` |
| 로컬 rename | watchdog `on_moved` | 새 path에 기존 version 유지 + `update(self.device_id)` |
| 원격 수신 (download) | poller | 로컬 version = drive appProperties vector 그대로 복사 |
| 원격 업로드 (upload) | engine | drive appProperties = `version.update(self.device_id)` |
| 원격 삭제 (tombstone move) | engine | drive appProperties 갱신 + `ot_sync_deleted=1` |

### 3.3 3-way 동기화 판정

```python
# src/reconciler.py: _decide 재구현 (PR3)
def decide(local: FileEntry | None, remote: FileEntry | None) -> Action:
    if local is None and remote is None:
        return NoOp()
    if local is None:
        return decide_download_or_delete(remote, presence="remote_only")
    if remote is None:
        return decide_upload_or_delete(local, presence="local_only")

    # 양쪽 존재
    if local.md5 == remote.md5 and local.size == remote.size:
        # 내용 동일 → version merge + 전송 생략
        return UpdateVectorOnly(merged=local.version.merge(remote.version))

    ordering = local.version.compare(remote.version)
    if ordering == VectorOrdering.Equal:
        return NoOp()
    if ordering == VectorOrdering.Greater:
        return Upload() if not local.deleted else DeleteRemote()
    if ordering == VectorOrdering.Lesser:
        return Download() if not remote.deleted else DeleteLocal()
    # Concurrent — Syncthing 충돌 규칙
    return resolve_conflict(local, remote)


def resolve_conflict(local, remote) -> Action:
    # PR #10207 반영: 삭제도 일반 이벤트. mtime 비교.
    if local.mtime > remote.mtime:
        return Upload(conflict_copy_of=remote)  # remote를 conflict copy로
    elif remote.mtime > local.mtime:
        return Download(conflict_copy_of=local)
    else:
        # 동률 — device prefix 큰 쪽이 패배
        local_dev = max(local.version.counters.keys()) if local.version.counters else ""
        remote_dev = max(remote.version.counters.keys()) if remote.version.counters else ""
        if local_dev > remote_dev:
            return Upload(conflict_copy_of=remote)
        return Download(conflict_copy_of=local)
```

### 3.4 드라이브 측 저장 — appProperties 30-key 분할

```
appProperties (예시):
  "ot_sync_schema":        "v2"                     # 스키마 버전 고정
  "ot_sync_deleted":       "0"                      # "1"이면 tombstone
  "ot_sync_vv_a3b4c5d6":   "1745000000123"          # 기기 a3b4c5d6의 HLC ms
  "ot_sync_vv_b1c2d3e4":   "1745000050456"
  "ot_sync_vv_c5d6e7f8":   "1744999999000"
  ...                                                # 최대 28개 기기
```

- `src/drive_vv_codec.py` 유틸로 직렬화/역직렬화.
- 기기 수 > 28 → `VersionVector.trim(28)` — 가장 작은 값 drop.
- 로드 실패 / 키 없음 → `VersionVector.empty()` fallback.

### 3.5 삭제 처리 — 드라이브 `.sync/tombstones/` 폴더

1. 로컬 `_do_delete_remote` → `drive.files.update(fileId, parents: .sync/tombstones/)` + appProperties.deleted=1.
2. 다른 기기 poller는 `parents` 변경을 Changes API로 감지 → `deleted=True`로 분류.
3. **Retention** (기본 90일, 설정 `tombstone_retention_days`):
   - `src/convergence.py`가 `.sync/convergence.json` (드라이브 루트 JSON 파일)을 읽어/쓰기.
   - 각 기기는 자신이 마지막으로 확인한 tombstone set을 기록.
   - 모든 활성 기기가 수렴 확인 + 90일 경과 → `files.delete` 실삭제.
   - 분실/영구 오프라인 기기는 blacklist (PR4).

### 3.6 로컬 삭제 처리 — `.sync/trash/{uuid}`

- Flat UUID 저장 (Windows MAX_PATH 회피).
- 메타데이터 파일 `.sync/trash/{uuid}.json`에 원본 경로, mtime, md5, deleted_at 기록.
- `trash_retention_days` (기본 30일) 경과 시 GC.

### 3.7 동기화 플로우

**부트 시 state 있음**:
1. `state.load()` → 인메모리 인덱스.
2. 로컬 변화 = `scan_local_files()` 결과 × 기존 state 비교 → version 증분 이벤트 생성.
3. `_classify_remote()` = Drive Changes API + appProperties 파싱 → 원격 version 수집.
4. path별 `decide(L, R)` → action 실행.

**부트 시 state 없음 — `run_without_state` 재설계 (PR3)**:
1. Drive `files.list(fields='files(id,name,parents,md5Checksum,appProperties,modifiedTime)')` → 모든 파일 + `.sync/tombstones/` 내용까지.
2. `scan_local_files()` — 로컬 파일은 version=empty로 시작.
3. path별 비교:
   - **양쪽 존재 + md5 동일**: `version = L.version.merge(R.version)` + 전송 생략.
   - **양쪽 존재 + md5 다름**: vector compare → `decide()`.
   - **한쪽만 존재**: 반대쪽 tombstone 참조 → vector compare.
   - **tombstone만 존재**: 로컬에 반영 (`deleted=True` 플래그 + 실제 파일은 없거나 로컬 trash로).

---

## 4. PR 로드맵

각 PR은 **독립 리뷰·병합 가능**. PR1 → PR2 → PR3 → PR4 순서로 의존.

### PR1 — Version Vector 로컬 도입 + 로컬 Tombstone

**목적**: 로컬 측에서 모든 이벤트가 vector 증분. 로컬 삭제는 `.sync/trash/`.

**신규 파일**:
- `src/version_vector.py` — §3.1 그대로.
- `src/trash.py` — `TrashManager(.sync/trash/{uuid})` + GC.
- `tests/test_version_vector.py`
- `tests/test_trash.py`

**수정 파일**:
- [src/state.py](../../src/state.py):
  - `FileEntry`에 `version`, `deleted`, `deleted_at`, `md5` 필드 추가 (md5는 PR3에서 활용).
  - `load()`에 v1→v2 자동 마이그레이션: 기존 파일은 `version=empty, deleted=False`.
  - `save()`에 신규 필드 직렬화.
  - `scan_local_files()` 호출자가 version을 기존 entry에서 복사하도록 보장.
  - `diff()`는 PR3에서 제거 예정이나 일단 유지.
- [src/sync_engine.py](../../src/sync_engine.py):
  - `_do_upload` 직후 `version.update(device_id)` 호출.
  - `_do_download` 직후 version 갱신 (PR2에서 원격 vector 반영으로 교체).
  - `_do_delete_remote` 직후 version.update + `deleted=True` 기록.
  - `_do_delete_local` 호출 시 **로컬 파일을 `trash_manager.move()` 로 이동**.
  - `handle_local_change("deleted")`: 기존 `ACTION_DELETE_REMOTE` 유지하되 version 증가.
  - `on_moved` 이벤트 처리 추가 (`ACTION_RENAME`?) — 복잡하면 delete+create로 분해.
- [src/local_watcher.py](../../src/local_watcher.py):
  - `on_moved` 이벤트 바인딩.
  - trash로 이동 시 `_mark_local_written`에 새 path도 추가해 에코 억제.
- [src/config.py](../../src/config.py):
  - `STATE_VERSION = 2`.
  - 신규 필드: `trash_retention_days: int = 30`.
- 기존 테스트들: v2 마이그레이션으로 인한 회귀 없도록 수정.

**구현 체크리스트**:
- [ ] `VersionVector` 구현 + 100% 커버리지 (empty/update/compare/merge/trim).
- [ ] HLC clock-skew 방어 테스트: `update()` 2회 연속 호출 시 value strict increase, 시간 역행 시에도 증가.
- [ ] `trim(28)` 동작: 30개 → 28개, 남는 것은 value 큰 상위.
- [ ] `FileEntry` v2 직렬화 왕복: to_dict → from_dict → 동일.
- [ ] v1 파일 자동 마이그레이션: 기존 `{"mtime":.., "size":..}`만 있는 파일 load 시 `version=empty, deleted=False`.
- [ ] `TrashManager.move(abs_path, rel_path)` → `.sync/trash/{uuid}` 저장 + 메타 JSON.
- [ ] `TrashManager.gc(now)` 30일 경과 파일 삭제.
- [ ] `should_ignore('.sync/trash/foo')` true 확인 (기존 ignore 패턴 검증).
- [ ] 로컬 삭제 통합 테스트: 파일 삭제 → drive delete → local file → trash 이동.
- [ ] rename 통합 테스트: `on_moved` → delete+create 분해로 정상 동작 (version 각각 증가).

**수동 검증**:
```bash
# 테스트 볼트에서:
echo "hello" > vault/test.md
sleep 6   # debounce
cat vault/.sync/sync_state.json | jq '.files["test.md"].version'
# → {"<my_device_prefix>": <ms>}  가 나와야 함
echo "world" >> vault/test.md
sleep 6
cat vault/.sync/sync_state.json | jq '.files["test.md"].version'
# → value가 증가해야 함
rm vault/test.md
sleep 6
ls vault/.sync/trash/   # → uuid 파일 + uuid.json 메타
```

### PR2 — 드라이브 appProperties 통합 + 드라이브 Tombstone 폴더

**목적**: 드라이브 측 version vector 저장/로드. 원격 삭제는 `.sync/tombstones/` move.

**신규 파일**:
- `src/drive_vv_codec.py` — `encode(VersionVector, deleted, md5) -> dict[str, str]`, `decode(appProperties) -> (VersionVector, deleted, md5)`. `trim(28)` 포함.
- `tests/test_drive_vv_codec.py`

**수정 파일**:
- [src/drive_client.py](../../src/drive_client.py):
  - `upload(local_path, rel_path, existing_id, vector, deleted=False, md5=None)` — `appProperties`에 vector 인코딩 전달.
  - `list_all_files()` 응답에 `appProperties` 포함 (fields 파라미터에 추가).
  - `get_changes()` 응답에 `appProperties`, `parents` 추가.
  - 신규: `move_to_tombstones(file_id)` — `parents` 업데이트 + appProperties.deleted=1.
  - 기존 `delete()`는 convergence 경과 후에만 호출되는 "hard delete" 용도로 이름 변경.
  - `.sync/tombstones/` 폴더 보장 유틸: `ensure_tombstones_folder()` → folder_id 반환.
- [src/sync_engine.py](../../src/sync_engine.py):
  - `_do_upload` → drive_client.upload에 version 전달.
  - `_do_delete_remote` → `move_to_tombstones` 사용.
  - `_do_download` → drive 응답의 appProperties로 로컬 version 설정.
  - `_change_to_action`에서 `parents`가 tombstones 폴더로 바뀐 change를 `ACTION_DELETE_LOCAL`로 분류.
- [src/poller.py](../../src/poller.py):
  - Changes API fields에 `parents`, `appProperties` 추가.

**구현 체크리스트**:
- [ ] `drive_vv_codec.encode/decode` 왕복 테스트.
- [ ] 30-key 초과 → trim(28) 검증.
- [ ] `ot_sync_schema=v2`가 없는 파일은 legacy로 취급 (version=empty).
- [ ] drive `.sync/tombstones/` 폴더 자동 생성.
- [ ] upload 시 appProperties 포함 실측 (실제 drive API 호출 테스트).
- [ ] `move_to_tombstones` 후 `files.get(parents)` 확인.
- [ ] Changes API에서 parents 변경이 감지되는지 검증.

**수동 검증**:
```bash
# 로컬에서 파일 생성 → 업로드 대기 → drive 웹에서 파일 속성 확인
# "고급" 탭에서 커스텀 속성 확인 (OAuth 앱 소유자만 보임)
# ot_sync_schema=v2, ot_sync_vv_<device>=<ms> 가 있어야 함

# 파일 삭제 → drive 웹에서 .sync/tombstones/ 폴더 확인
```

### PR3 — 3-way Reconciler 재설계 + 충돌 해결 + md5

**목적**: `compare` 기반 action 결정. Syncthing 충돌 규칙 적용. content hash 통합.

**신규 파일**:
- `src/hash.py` — 청크 md5 계산, ≤100MB 제한, 초과 시 None.
- `tests/test_reconciler_v2.py`

**수정 파일**:
- [src/reconciler.py](../../src/reconciler.py) — 전면 재작성:
  - `_classify_local/_classify_remote` 제거 또는 단순화.
  - `_decide` → §3.3의 version compare 기반.
  - `run()` 은 `state.files` × `scan_local_files` × drive Changes를 path별로 decide.
  - `run_without_state()` → §3.7의 재설계 플로우.
- [src/sync_engine.py](../../src/sync_engine.py) — 모든 `_do_*`에서 md5 기록/사용.
- [src/state.py](../../src/state.py) — `diff()` 더 이상 사용 안 하면 deprecated 처리.
- [src/conflict.py](../../src/conflict.py) — 파일명 포맷을 Syncthing 스타일로:
  - 기존: `{stem}.conflict-{device_id}-{ts}.{ext}`
  - 신규: `{stem}.sync-conflict-{ts}-{device_prefix}.{ext}`
- [src/config.py](../../src/config.py): `hash_max_file_size_mb: int = 100`, `hash_verification: bool = True`.

**구현 체크리스트**:
- [ ] `hash.compute_md5(path, max_bytes)` 청크 단위 계산 + 크기 초과 시 None.
- [ ] 모든 _do_upload/download 성공 후 `FileEntry.md5` 기록.
- [ ] `decide()` 에 대한 단위 테스트: Equal/Greater/Lesser/Concurrent 각 시나리오.
- [ ] 충돌 해결 테스트: 양쪽 수정 시 mtime 큰 쪽 승, 동률이면 device 큰 쪽 패배.
- [ ] `run_without_state` 시나리오:
  - 양쪽 동일 (md5 일치) → 전송 없음.
  - 로컬 unique → upload.
  - 원격 unique → download.
  - 로컬 unique + 원격 tombstone + md5 일치 → local delete_local.
  - 원격 unique + `ot_sync_deleted=0` → download.
- [ ] 증상 3, 4 E2E 테스트: 삭제 후 재시작 시 부활 없음.
- [ ] drive 메타데이터만 변경된 경우 (md5 동일, modifiedTime 다름) → download skip.

**수동 검증**:
```
# 증상 3 재현 테스트:
#   기기 A: 파일 삭제 → drive tombstone move
#   기기 A: 앱 종료 → 재시작
#   기기 A의 로컬에 파일 없음, drive에 tombstone 있음
#   → 앱이 부활시키지 않아야 함

# 증상 4 재현 테스트:
#   기기 B (drive web)에서 파일 삭제
#   기기 A: 폴링 주기 후 파일이 .sync/trash/로 이동
#   기기 A: 앱 재시작
#   → 로컬에 파일 없음, drive에 tombstone 있음, 부활 없음

# 충돌 테스트:
#   기기 A 오프라인 → 파일 편집
#   기기 B 오프라인 → 같은 파일 편집
#   기기 A 온라인 → upload
#   기기 B 온라인 → concurrent 감지 → B의 로컬이 .sync-conflict-*로 이름 변경 + drive 버전 download
```

### PR4 — Intent Log + Convergence + 설정 완성

**목적**: 부분 실패 복구, tombstone 안전 GC.

**신규 파일**:
- `src/intent_log.py` — `IntentLog(append_path)`, `IntentLog.replay(engine)`.
- `src/convergence.py` — `.sync/convergence.json` 드라이브 파일 읽기/쓰기.
- `tests/test_intent_log.py`
- `tests/test_convergence.py`

**수정 파일**:
- [src/sync_engine.py](../../src/sync_engine.py):
  - `_run_action` 전에 `intent_log.record(action)`.
  - action 성공 후 `intent_log.resolve(intent_id)`.
  - 시작 시 `intent_log.replay()` 호출.
- [src/main.py](../../src/main.py) — 초기화 시 IntentLog + ConvergenceManager wiring.
- [src/config.py](../../src/config.py): `tombstone_retention_days: int = 90`.

**구현 체크리스트**:
- [ ] Intent record/resolve JSONL 라인 단위 append.
- [ ] SIGKILL 시뮬레이션 테스트: delete 직후 kill → 재시작 시 intent replay 확인.
- [ ] `convergence.json` 경합 조건 (optimistic concurrency with `head` 필드 또는 etag).
- [ ] 모든 기기 수렴 확인 후에만 hard delete.
- [ ] 90일 경과하지 않았거나 미수렴 기기 있으면 tombstone 유지.
- [ ] 설정 옵션 모두 config.yaml에 기본값 포함.

**수동 검증**:
```bash
# 프로세스 킬 복구:
#   python -m src.main &
#   PID=$!
#   rm vault/test.md    # triggers delete_remote
#   kill -9 $PID        # immediately
#   python -m src.main  # should replay intent
#   check drive: test.md should be in .sync/tombstones/
```

---

## 5. 잔여 위험 매트릭스

| ID | 위험 | 대응 | PR |
|---|---|---|---|
| R1 | 기기 간 시계 편차 | HLC `max(existing+1, unix_ms)` | PR1 |
| R2 | mtime 조작 | md5+size content match 우선 | PR3 |
| R3 | 같은 경로 삭제→재생성 (다른 내용) | vector.update 새로 발행, deleted=False 복귀 | PR1 |
| R4 | drive 메타만 변경 → modifiedTime 갱신 | md5 동일 → download skip | PR3 |
| R5 | Rename vs Delete+Create 구별 | `on_moved` 처리 + md5+size 매칭 | PR1 (기본) |
| R6 | 부분 실패 (delete 성공 → state 저장 실패) | Intent log WAL | PR4 |
| R7 | 다수 기기 동시 삭제 + 제3자 수정 | Concurrent → Syncthing conflict 규칙 | PR3 |
| R8 | Tombstone retention 경과 후 유령 부활 | `.sync/tombstones/` + `.sync/convergence.json` | PR4 |
| R9a | `.sync/trash/` 경로 길이 (Windows MAX_PATH) | flat UUID 저장 | PR1 |
| R9b | trash 이동 → watcher 반향 | `on_moved` 에코 억제 | PR1 |
| R9c | `.sync/` ignore 누락 시 재앙 | `should_ignore` 검증 + 테스트 | PR1 |
| R9d | trash/tombstones 용량 | retention GC | PR1/PR4 |
| R10 | 대용량 파일 md5 비용 | `hash_max_file_size_mb: 100` fallback | PR3 |
| R11 | 29+ 기기 시나리오 | `trim(28)` — 오래된 카운터 drop | PR1/PR2 |
| R12 | appProperties 30-key 고갈 | 기기별 key 분리 + schema/deleted 예약 | PR2 |
| R13 | drive 내부 move 감지 지연 | Changes API `parents` 명시 poll | PR2 |

**근본 한계 (현실적으로 남는 위험)**:
- R11: 29 기기 초과 시 가장 오래된 기기 카운터 유실. 개인 볼트 시나리오에선 사실상 비현실적이나, 공유 볼트 확장 시 재검토 필요.
- R8 완전 해결은 영구 분실 기기의 blacklist 정책이 성숙한 뒤에만 가능. 초기 버전은 90일 고정 + 수동 blacklist (PR4).

---

## 6. 검증 계획

### 6.1 자동 테스트 (pytest)

```bash
pytest tests/ -v
pytest tests/test_version_vector.py -v         # PR1: HLC, compare, trim
pytest tests/test_state.py -v                  # PR1: v1→v2 마이그레이션
pytest tests/test_trash.py -v                  # PR1
pytest tests/test_drive_vv_codec.py -v         # PR2: appProperties 왕복
pytest tests/test_reconciler_v2.py -v          # PR3: version compare
pytest tests/test_conflict.py -v               # PR3: Syncthing naming
pytest tests/test_intent_log.py -v             # PR4
pytest tests/test_convergence.py -v            # PR4
pytest tests/test_e2e_smoke.py -v              # 증상 1~4 end-to-end
```

### 6.2 수동 시나리오

| # | 시나리오 | 예상 동작 | 해결하는 증상 |
|---|---|---|---|
| 1 | B가 drive에 X 업로드 → A 접속 | A가 download (삭제 아님) | 1 |
| 2 | A에 X 생성 | drive에 upload (삭제 아님) | 2 |
| 3 | A 로컬에서 X 삭제 → 재시작 | drive tombstone, 부활 없음 | 3 |
| 4 | drive에서 X 삭제 → A 재시작 | 로컬 trash 이동, 부활 없음 | 4 |
| 5 | A 시계 1시간 빠름 → A 수정 → B 수정 | HLC로 B의 수정이 이김 | R1 |
| 6 | 파일 삭제 → 같은 경로에 다른 내용 생성 | 업로드 성공, tombstone 해제 | R3 |
| 7 | A/B 동시 편집 | `.sync-conflict-*` 파일 생성 | R7 |
| 8 | `_do_delete_remote` 직후 SIGKILL → 재시작 | intent replay → tombstone 복구 | R6 |

### 6.3 멀티 디바이스 시뮬레이션

3개 로컬 볼트 + 각각의 state로 동일 drive folder에 대해 순차 실행:
- version vector 전파 확인
- concurrent 감지
- tombstone 수렴
- 기기 간 device prefix 충돌 (8자 hex 충돌 확률 매우 낮으나 테스트 필요)

---

## 7. 수정 대상 파일 전체 목록

| 파일 | 역할 | PR |
|---|---|---|
| `src/version_vector.py` (신규) | HLC vector, compare, trim | 1 |
| `src/trash.py` (신규) | 로컬 `.sync/trash/` | 1 |
| `src/drive_vv_codec.py` (신규) | appProperties ↔ vector | 2 |
| `src/hash.py` (신규) | 로컬 md5 | 3 |
| `src/intent_log.py` (신규) | WAL | 4 |
| `src/convergence.py` (신규) | drive `.sync/convergence.json` | 4 |
| [src/state.py](../../src/state.py) | v2 스키마, version, deleted | 1,3 |
| [src/sync_engine.py](../../src/sync_engine.py) | 모든 action에 vector 증분, tombstone move | 1,2,3,4 |
| [src/reconciler.py](../../src/reconciler.py) | compare 기반 decide, run_without_state 재설계 | 3 |
| [src/drive_client.py](../../src/drive_client.py) | appProperties 포함, tombstone folder | 2 |
| [src/poller.py](../../src/poller.py) | parents 변경 감지 | 2 |
| [src/conflict.py](../../src/conflict.py) | Syncthing 명명 | 3 |
| [src/local_watcher.py](../../src/local_watcher.py) | on_moved, trash 에코 | 1 |
| [src/config.py](../../src/config.py) | 신규 설정, STATE_VERSION=2 | 1,3,4 |
| [src/main.py](../../src/main.py) | IntentLog / Convergence 와이어링 | 4 |

---

## 8. 구현 착수 체크리스트 (세션 인수인계)

다음 세션에서 PR1 착수 전 확인할 것:

- [ ] 이 문서 전체 정독.
- [ ] [config.yaml](../../config.yaml) 현재 구조 확인.
- [ ] 기존 `sync_state.json` 샘플을 확보 (마이그레이션 검증용).
- [ ] Google Drive API 토큰 정상 동작 확인 (`python -m src.main --config config.yaml`).
- [ ] 테스트 드라이브 폴더 준비 (증상 재현용 볼트 별도로).
- [ ] feature 브랜치 분기: `git checkout -b feat/version-vector-pr1`.

참고 링크 (반드시 설계 변경 전 재확인):
- [Syncthing lib/protocol/vector.go](https://raw.githubusercontent.com/syncthing/syncthing/main/lib/protocol/vector.go)
- [Syncthing PR #10207](https://github.com/syncthing/syncthing/pull/10207)
- [Google Drive API — Properties](https://developers.google.com/workspace/drive/api/guides/properties)
- [Google Drive API — files.update](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update)
- [Google Drive API — changes.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list)
