"""Google Drive API v3 래퍼.

동기화 로직 없이 순수 API 호출만 담당한다.
인증, 파일 조작, 변경 감지, 폴더 관리 기능을 제공한다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import googleapiclient.discovery
import googleapiclient.http
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError

from src.config import SyncConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
MIME_FOLDER = "application/vnd.google-apps.folder"

# ── 오류 처리 정책 상수 ────────────────────────────────────────────────────
RETRY_MAX_ATTEMPTS = 3          # 네트워크/5xx 재시도 횟수
RETRY_BASE_DELAY = 1.0          # 재시도 기본 간격 (지수: 1→2→4s)
RATE_LIMIT_MAX_DELAY = 300.0    # 429 최대 대기시간 (5분)
RATE_LIMIT_MAX_ATTEMPTS = 10    # 429 무한 루프 방지


class TokenInvalidError(Exception):
    """Drive Changes API page_token이 무효화됨 (410 Gone). 재발급 필요."""


def _http_status(error: HttpError) -> int | None:
    """HttpError에서 정수 status 코드를 추출한다."""
    raw = getattr(error.resp, "status", None)
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _execute_with_retry(request: Any, *, description: str = "drive_api") -> Any:
    """Drive API 요청을 재시도 정책과 함께 실행한다.

    정책:
    - 429 Too Many Requests: 지수 백오프 1→2→4→…→300s, 최대 10회.
    - 5xx 서버 오류: 최대 3회 재시도 (1→2→4s).
    - 네트워크/IO 오류: 최대 3회 재시도 (1→2→4s).
    - 401/403: 즉시 전파 (자격증명 문제).
    - 410 Gone: TokenInvalidError로 변환.
    - 기타 4xx: 즉시 전파.
    """
    attempt = 0
    rate_attempt = 0
    rate_delay = RETRY_BASE_DELAY

    while True:
        try:
            return request.execute()
        except HttpError as e:
            status = _http_status(e)

            if status in (401, 403):
                logger.error(f"{description} 자격증명 문제: HTTP {status}")
                raise

            if status == 410:
                logger.warning(f"{description} page_token 무효(410 Gone) — 재발급 필요")
                raise TokenInvalidError(str(e)) from e

            if status == 429:
                if rate_attempt >= RATE_LIMIT_MAX_ATTEMPTS:
                    logger.error(f"{description} 429 재시도 한계 초과")
                    raise
                wait = min(rate_delay, RATE_LIMIT_MAX_DELAY)
                logger.warning(f"{description} 429 Too Many Requests — {wait}s 대기")
                time.sleep(wait)
                rate_delay = min(rate_delay * 2, RATE_LIMIT_MAX_DELAY)
                rate_attempt += 1
                continue

            if status is not None and 500 <= status < 600:
                if attempt >= RETRY_MAX_ATTEMPTS:
                    logger.error(f"{description} {status} 재시도 한계 초과")
                    raise
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"{description} HTTP {status} — {wait}s 후 재시도")
                time.sleep(wait)
                attempt += 1
                continue

            # 기타 4xx: 재시도하지 않음
            raise

        except (OSError, TimeoutError) as e:
            if attempt >= RETRY_MAX_ATTEMPTS:
                logger.error(f"{description} 네트워크 재시도 한계 초과: {e}")
                raise
            wait = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(f"{description} 네트워크 오류 — {wait}s 후 재시도: {e}")
            time.sleep(wait)
            attempt += 1


class DriveClient:
    """Google Drive API v3 래퍼.

    동기화 로직 없이 순수 API 호출만 담당한다.
    폴더 ID는 세션 내 캐시(_folder_cache)로 관리하며,
    프로그램 재시작 시 Drive API를 다시 조회한다.
    """

    def __init__(self, config: SyncConfig) -> None:
        self._config = config
        self._folder_id = config.drive_folder_id
        self._credentials_file = config.credentials_file
        self._token_file = config.token_file
        self._service: Any = None

        # 세션 내 폴더 ID 캐시: rel_posix_path → drive_folder_id
        # 재시작 시 ensure_folder_path() 호출마다 Drive API 재조회됨
        self._folder_cache: dict[str, str] = {}

        # 볼트 트리에 속하는 것으로 확인된 폴더 ID 집합 (rel_path 없이 ID만)
        # _folder_cache.values()의 상위 집합. _is_under_vault() 성공 시 등록됨.
        self._vault_folder_ids: set[str] = {config.drive_folder_id}

        # 볼트 트리에 속하지 않는 것으로 확인된 폴더 ID 캐시 (반복 조회 방지)
        self._non_vault_ids: set[str] = set()

    # ── 인증 ──────────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """OAuth2 인증을 수행한다.

        첫 실행 시 브라우저를 열어 동의를 받고,
        이후에는 토큰을 자동 갱신한다.
        """
        creds: Credentials | None = None

        if self._token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_file), SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("OAuth 토큰 갱신 완료")
            else:
                if not self._credentials_file.exists():
                    raise FileNotFoundError(
                        f"credentials.json을 찾을 수 없습니다: "
                        f"'{self._credentials_file}'. "
                        "Google Cloud Console → APIs & Services → "
                        "Credentials에서 다운로드하세요."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_file), SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("OAuth 동의 완료")

            self._token_file.write_text(creds.to_json(), encoding="utf-8")
            logger.info(f"토큰 저장: {self._token_file}")

        self._service = googleapiclient.discovery.build(
            "drive", "v3", credentials=creds
        )
        logger.info("Google Drive 서비스 준비 완료")

    # ── 파일 조작 ─────────────────────────────────────────────────────────

    def upload(
        self,
        local_path: Path,
        relative_path: str,
        existing_id: str | None = None,
    ) -> str:
        """로컬 파일을 Drive에 업로드한다.

        existing_id가 있으면 update, 없으면 create.
        반환값: Drive 파일 ID.
        """
        media = googleapiclient.http.MediaFileUpload(
            str(local_path), resumable=False
        )

        if existing_id:
            self._service.files().update(
                fileId=existing_id,
                body={"name": local_path.name},
                media_body=media,
            ).execute()
            logger.info(f"Drive 업데이트: {relative_path}")
            return existing_id
        else:
            # 부모 폴더 확보
            parent_rel = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
            parent_id = self.ensure_folder_path(parent_rel)

            meta = {"name": local_path.name, "parents": [parent_id]}
            result = (
                self._service.files()
                .create(body=meta, media_body=media, fields="id")
                .execute()
            )
            file_id = result["id"]
            logger.info(f"Drive 업로드: {relative_path} (id={file_id})")
            return file_id

    def download(self, drive_file_id: str, local_path: Path) -> None:
        """Drive 파일을 로컬 경로에 다운로드한다."""
        local_path.parent.mkdir(parents=True, exist_ok=True)

        request = self._service.files().get_media(fileId=drive_file_id)
        content = request.execute()
        local_path.write_bytes(content)
        logger.info(f"Drive 다운로드: {local_path}")

    def delete(self, drive_file_id: str) -> None:
        """Drive 파일을 휴지통으로 이동한다 (소프트 삭제).

        참고: trashed=True는 Changes API에서 removed=True가 아니라
        file.trashed=True로 감지된다. get_changes()에서 양쪽 모두 처리한다.
        """
        self._service.files().update(
            fileId=drive_file_id, body={"trashed": True}
        ).execute()
        logger.info(f"Drive 휴지통 이동: {drive_file_id}")

    def rename(self, drive_file_id: str, new_name: str) -> None:
        """Drive 파일의 이름을 변경한다."""
        self._service.files().update(
            fileId=drive_file_id, body={"name": new_name}
        ).execute()
        logger.info(f"Drive 이름 변경: {drive_file_id} → {new_name}")

    def move(
        self,
        drive_file_id: str,
        new_parent_id: str,
        new_name: str | None = None,
    ) -> None:
        """Drive 파일을 다른 폴더로 이동한다. 이름 변경도 동시에 가능."""
        meta = (
            self._service.files()
            .get(fileId=drive_file_id, fields="parents")
            .execute()
        )
        old_parents = ",".join(meta.get("parents", []))

        body: dict[str, str] = {}
        if new_name:
            body["name"] = new_name

        self._service.files().update(
            fileId=drive_file_id,
            addParents=new_parent_id,
            removeParents=old_parents,
            body=body,
            fields="id,parents",
        ).execute()
        logger.info(f"Drive 이동: {drive_file_id} → parent={new_parent_id}")

    def get_file_metadata(
        self,
        drive_file_id: str,
        fields: str = "id,name,modifiedTime,size,parents",
    ) -> dict:
        """Drive 파일의 메타데이터를 가져온다."""
        return (
            self._service.files()
            .get(fileId=drive_file_id, fields=fields)
            .execute()
        )

    # ── 폴더 조작 ─────────────────────────────────────────────────────────

    def find_folder(self, name: str, parent_id: str) -> str | None:
        """부모 폴더 아래에서 이름으로 폴더를 찾는다. 없으면 None."""
        safe_name = name.replace("'", "\\'")
        q = (
            f"name='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='{MIME_FOLDER}' and trashed=false"
        )
        resp = (
            self._service.files()
            .list(q=q, fields="files(id)", pageSize=1)
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str) -> str:
        """부모 폴더 아래에 새 폴더를 생성한다. 반환값: 폴더 ID."""
        meta = {
            "name": name,
            "mimeType": MIME_FOLDER,
            "parents": [parent_id],
        }
        result = (
            self._service.files()
            .create(body=meta, fields="id")
            .execute()
        )
        folder_id = result["id"]
        logger.info(f"Drive 폴더 생성: {name} (id={folder_id})")
        return folder_id

    def ensure_folder_path(self, rel_folder_path: str) -> str:
        """상대 경로의 폴더 계층을 Drive에 보장한다.

        예: "notes/archive" → notes/ 확인/생성 → archive/ 확인/생성.
        반환값: 가장 깊은 폴더의 Drive ID.
        """
        if not rel_folder_path or rel_folder_path == ".":
            return self._folder_id

        # 캐시 확인
        cached = self._folder_cache.get(rel_folder_path)
        if cached:
            return cached

        parts = rel_folder_path.split("/")
        parent_id = self._folder_id
        built = ""

        for part in parts:
            built = f"{built}/{part}".lstrip("/")
            folder_id = self._folder_cache.get(built)

            if not folder_id:
                folder_id = self.find_folder(part, parent_id)
                if not folder_id:
                    folder_id = self.create_folder(part, parent_id)
                self._folder_cache[built] = folder_id
                self._vault_folder_ids.add(folder_id)

            parent_id = folder_id

        return parent_id

    # ── 변경 감지 ─────────────────────────────────────────────────────────

    def get_initial_token(self) -> str:
        """현재 시점의 Changes API 시작 토큰을 발급받는다."""
        request = self._service.changes().getStartPageToken()
        result = _execute_with_retry(request, description="get_initial_token")
        token = result["startPageToken"]
        logger.info(f"Changes API 시작 토큰: {token}")
        return token

    def get_changes(self, page_token: str) -> tuple[list[dict], str]:
        """page_token 이후의 변경 목록을 가져온다.

        볼트 폴더 범위 필터링:
        - Changes API는 계정 전체 변경을 반환하므로,
          file.parents를 확인하여 볼트 폴더 트리에 속하는 것만 포함한다.

        삭제 판정:
        - removed=True → 삭제
        - removed=False이지만 file.trashed=True → 삭제

        폴더 필터링:
        - mimeType=folder인 변경은 파일 목록에서 제외
        - 단, 볼트 안의 새 폴더는 _folder_cache에 등록

        반환값: (변경 목록, 새 page_token)
        """
        all_changes: list[dict] = []
        current_token = page_token
        new_token = page_token

        while current_token:
            request = self._service.changes().list(
                pageToken=current_token,
                spaces="drive",
                fields=(
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,"
                    "file(id,name,mimeType,modifiedTime,parents,trashed,"
                    "size,md5Checksum))"
                ),
                includeRemoved=True,
                pageSize=100,
            )
            resp = _execute_with_retry(request, description="get_changes")

            for change in resp.get("changes", []):
                normalized = self._normalize_change(change)
                if normalized is not None:
                    all_changes.append(normalized)

            if "newStartPageToken" in resp:
                new_token = resp["newStartPageToken"]
                current_token = None
            else:
                current_token = resp.get("nextPageToken")

        logger.debug(f"Changes API: {len(all_changes)}개 변경 감지")
        return all_changes, new_token

    def _normalize_change(self, change: dict) -> dict | None:
        """Drive 변경 하나를 정규화한다.

        볼트 폴더 밖의 변경, 폴더 변경은 제외한다.
        반환값: 정규화된 dict 또는 None (무시).
        """
        file_id = change.get("fileId", "")
        removed = change.get("removed", False)
        file_meta = change.get("file")

        # removed=True이고 메타데이터 없는 경우
        # 볼트 파일인지 확인: _folder_cache 값 또는 _vault_folder_ids에 존재하는지 검사
        if removed and not file_meta:
            cached_file_ids = set(self._folder_cache.values())
            if file_id in cached_file_ids or file_id in self._vault_folder_ids:
                return {
                    "file_id": file_id,
                    "removed": True,
                    "file": None,
                }
            # 볼트 밖 파일의 삭제 → 무시
            return None

        if not file_meta:
            return None

        # 볼트 폴더 범위 필터링
        parents = file_meta.get("parents", [])
        if not self._is_in_vault(file_id, parents):
            return None

        is_folder = file_meta.get("mimeType") == MIME_FOLDER
        is_trashed = file_meta.get("trashed", False)

        # 폴더 변경은 파일 목록에서 제외하되, 볼트 폴더 ID 집합에 등록
        if is_folder and not is_trashed:
            self._vault_folder_ids.add(file_id)
            return None

        # 삭제 판정: removed=True OR trashed=True
        is_deleted = removed or is_trashed

        # 소비자(sync engine) 용 정규화된 스키마
        # Google Docs(.gdoc 등)는 md5Checksum이 없어 None이 될 수 있다.
        file_payload: dict | None
        if is_deleted:
            file_payload = None
        else:
            file_payload = {
                "name": file_meta.get("name"),
                "modified_time": file_meta.get("modifiedTime"),
                "md5": file_meta.get("md5Checksum"),
            }

        return {
            "file_id": file_id,
            "removed": is_deleted,
            "file": file_payload,
        }

    def _is_in_vault(self, file_id: str, parents: list[str]) -> bool:
        """파일의 parents가 볼트 폴더 트리에 속하는지 확인한다.

        확인 순서:
        1) parent가 _vault_folder_ids에 있음 → True (루트 + 캐시된 모든 볼트 폴더)
        2) parent가 _non_vault_ids에 있음 → 스킵
        3) 위 두 경우 아님 → _is_under_vault()로 재귀 조회
        """
        if not parents:
            return False

        for parent_id in parents:
            # 1) 이미 볼트 트리로 확인된 폴더 (루트 포함)
            if parent_id in self._vault_folder_ids:
                return True

            # 2) non-vault 캐시에 있으면 빠르게 스킵
            if parent_id in self._non_vault_ids:
                continue

            # 3) Drive API로 재귀 확인
            if self._is_under_vault(parent_id):
                return True

        return False

    def _is_under_vault(self, folder_id: str) -> bool:
        """주어진 폴더 ID가 볼트 루트 폴더의 하위인지 재귀 확인한다.

        루트에 도달하면 True + 경로상 모든 폴더를 _vault_folder_ids에 등록.
        My Drive 루트에 도달하면 False + _non_vault_ids에 캐싱.
        """
        # 이미 확인된 폴더
        if folder_id in self._vault_folder_ids:
            return True
        if folder_id in self._non_vault_ids:
            return False

        # 재귀 탐색을 위한 경로 추적
        path_ids: list[str] = [folder_id]
        current_id = folder_id

        while True:
            try:
                meta = (
                    self._service.files()
                    .get(fileId=current_id, fields="id,name,parents")
                    .execute()
                )
            except Exception:
                logger.debug(f"폴더 조회 실패: {current_id}")
                # 조회 실패 → 볼트 밖으로 간주
                self._non_vault_ids.update(path_ids)
                return False

            parents = meta.get("parents", [])
            if not parents:
                # 최상위(My Drive 루트)에 도달 → 볼트 밖
                self._non_vault_ids.update(path_ids)
                return False

            parent_id = parents[0]

            if parent_id in self._vault_folder_ids:
                # 볼트 트리에 도달 → 경로상 모든 폴더를 볼트 ID 집합에 등록
                self._vault_folder_ids.update(path_ids)
                return True

            if parent_id in self._non_vault_ids:
                # 볼트 밖으로 확인된 폴더에 도달
                self._non_vault_ids.update(path_ids)
                return False

            path_ids.append(parent_id)
            current_id = parent_id

    # ── 전체 목록 ─────────────────────────────────────────────────────────

    def list_all_files(self) -> list[dict]:
        """Drive 루트 폴더 아래의 전체 파일 목록을 가져온다.

        BFS(너비 우선 탐색)로 폴더 트리를 순회하며,
        각 파일에 relative_path를 계산하여 포함한다.
        폴더 자체는 결과에서 제외하고 파일만 반환한다.
        _folder_cache에 탐색 중 발견된 폴더 ID도 등록한다.
        """
        result: list[dict] = []

        # BFS 큐: (drive_folder_id, rel_path_prefix)
        queue: deque[tuple[str, str]] = deque()
        queue.append((self._folder_id, ""))

        while queue:
            current_folder_id, path_prefix = queue.popleft()
            page_token: str | None = None

            while True:
                q = f"'{current_folder_id}' in parents and trashed=false"
                resp = (
                    self._service.files()
                    .list(
                        q=q,
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,size,parents)",
                        pageSize=100,
                        pageToken=page_token,
                    )
                    .execute()
                )

                for item in resp.get("files", []):
                    name = item["name"]
                    rel_path = f"{path_prefix}{name}" if not path_prefix else f"{path_prefix}/{name}"

                    # 경로 보정: 루트 직계 자식일 때 prefix가 빈 문자열
                    if not path_prefix:
                        rel_path = name

                    if item.get("mimeType") == MIME_FOLDER:
                        # 폴더 → 큐에 추가 + 캐시 등록
                        queue.append((item["id"], rel_path))
                        self._folder_cache[rel_path] = item["id"]
                        self._vault_folder_ids.add(item["id"])
                    else:
                        # 파일 → 결과에 추가
                        item["relative_path"] = rel_path
                        result.append(item)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        logger.info(f"Drive 전체 파일 목록: {len(result)}개")
        return result

    # ── 프로퍼티 ──────────────────────────────────────────────────────────

    @property
    def root_folder_id(self) -> str:
        """Drive 루트 폴더 ID를 반환한다."""
        return self._folder_id
