"""Google Drive API v3 래퍼.

동기화 로직 없이 순수 API 호출만 담당한다.
인증, 파일 조작, 변경 감지, 폴더 관리 기능을 제공한다.
"""
from __future__ import annotations

import io
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import googleapiclient.discovery
import googleapiclient.http
from google.auth.exceptions import RefreshError
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


class DriveFileNotFoundError(Exception):
    """Drive 파일이 404 Not Found — 상태 파일에서 해당 drive_id 제거 필요."""

    def __init__(self, file_id: str, message: str | None = None) -> None:
        self.file_id = file_id
        super().__init__(message or f"Drive 파일을 찾을 수 없음: {file_id}")


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


def _execute_with_retry(
    request: Any,
    *,
    description: str = "drive_api",
    not_found_file_id: str | None = None,
) -> Any:
    """Drive API 요청을 재시도 정책과 함께 실행한다.

    정책:
    - 429 Too Many Requests: 지수 백오프 1→2→4→…→300s, 최대 10회.
    - 5xx 서버 오류: 최대 3회 재시도 (1→2→4s).
    - 네트워크/IO 오류: 최대 3회 재시도 (1→2→4s).
    - 401/403: 즉시 전파 (자격증명 문제).
    - 410 Gone: TokenInvalidError로 변환.
    - 404: `not_found_file_id`가 지정된 경우 `DriveFileNotFoundError`로 변환.
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

            if status == 404 and not_found_file_id is not None:
                logger.warning(
                    f"{description} 404 Not Found: {not_found_file_id}"
                )
                raise DriveFileNotFoundError(not_found_file_id, str(e)) from e

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

        # 역매핑: drive_folder_id → rel_posix_path (루트는 "")
        # parents 기반 경로 재구성(resolve_vault_rel_path)에서 사용.
        self._folder_id_to_rel: dict[str, str] = {config.drive_folder_id: ""}

        # 볼트 트리에 속하는 것으로 확인된 폴더 ID 집합 (rel_path 없이 ID만)
        # _folder_cache.values()의 상위 집합. _is_under_vault() 성공 시 등록됨.
        self._vault_folder_ids: set[str] = {config.drive_folder_id}

        # 볼트 트리에 속하지 않는 것으로 확인된 폴더 ID 캐시 (반복 조회 방지)
        self._non_vault_ids: set[str] = set()

        # .sync/tombstones/ 폴더 ID 캐시 (세션 내 1회 조회)
        self._tombstones_folder_id: str | None = None

        # .sync/convergence.json 파일 ID 캐시 (세션 내 1회 조회/생성)
        self._convergence_file_id: str | None = None

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
            refreshed = False
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info("OAuth 토큰 갱신 완료")
                    refreshed = True
                except RefreshError:
                    # refresh_token이 revoke되거나 만료된 경우 — 브라우저 재인증으로 폴백
                    logger.warning(
                        "OAuth refresh 실패 (토큰 revoke/만료) → 브라우저 재인증 진행"
                    )
                    try:
                        self._token_file.unlink()
                    except OSError:
                        pass
                    creds = None

            if not refreshed:
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
        *,
        app_properties: dict[str, str] | None = None,
    ) -> dict:
        """로컬 파일을 Drive에 업로드한다.

        existing_id가 있으면 update, 없으면 create.
        app_properties가 주어지면 appProperties에 포함한다.
        반환값: Drive 파일 메타데이터 dict (id, md5Checksum 등).
        """
        media = googleapiclient.http.MediaFileUpload(
            str(local_path), resumable=False
        )

        if existing_id:
            body: dict[str, Any] = {"name": local_path.name}
            if app_properties:
                body["appProperties"] = app_properties
            request = self._service.files().update(
                fileId=existing_id,
                body=body,
                media_body=media,
                fields="id,md5Checksum,appProperties",
            )
            result = _execute_with_retry(
                request,
                description=f"upload.update[{relative_path}]",
                not_found_file_id=existing_id,
            )
            logger.info(f"Drive 업데이트: {relative_path}")
            return result
        else:
            # 부모 폴더 확보
            parent_rel = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
            parent_id = self.ensure_folder_path(parent_rel)

            meta: dict[str, Any] = {"name": local_path.name, "parents": [parent_id]}
            if app_properties:
                meta["appProperties"] = app_properties
            request = (
                self._service.files()
                .create(body=meta, media_body=media, fields="id,md5Checksum,appProperties")
            )
            result = _execute_with_retry(
                request, description=f"upload.create[{relative_path}]"
            )
            file_id = result["id"]
            logger.info(f"Drive 업로드: {relative_path} (id={file_id})")
            return result

    def download(self, drive_file_id: str, local_path: Path) -> dict:
        """Drive 파일을 로컬 경로에 다운로드한다.

        반환값: 파일 메타데이터 dict (md5Checksum, appProperties 등).
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)

        request = self._service.files().get_media(fileId=drive_file_id)
        content = _execute_with_retry(
            request,
            description=f"download[{drive_file_id}]",
            not_found_file_id=drive_file_id,
        )
        local_path.write_bytes(content)

        # get_media는 바이너리만 반환하므로 메타데이터는 별도 조회
        meta = self.get_file_metadata(
            drive_file_id,
            fields="id,md5Checksum,appProperties",
        )
        logger.info(f"Drive 다운로드: {local_path}")
        return meta

    def hard_delete(self, drive_file_id: str) -> None:
        """Drive 파일을 휴지통으로 이동한다 (convergence 후 최종 삭제용).

        참고: trashed=True는 Changes API에서 removed=True가 아니라
        file.trashed=True로 감지된다. get_changes()에서 양쪽 모두 처리한다.
        """
        request = self._service.files().update(
            fileId=drive_file_id, body={"trashed": True}
        )
        _execute_with_retry(
            request,
            description=f"hard_delete[{drive_file_id}]",
            not_found_file_id=drive_file_id,
        )
        logger.info(f"Drive 휴지통 이동 (hard_delete): {drive_file_id}")

    def move_to_tombstones(
        self,
        drive_file_id: str,
        app_properties: dict[str, str] | None = None,
    ) -> None:
        """Drive 파일을 .sync/tombstones/ 폴더로 이동한다 (논리 삭제).

        parents를 tombstones 폴더로 변경하고,
        appProperties에 deleted=1을 설정한다.
        """
        tombstones_id = self.ensure_tombstones_folder()

        # 현재 parents 조회
        meta = (
            self._service.files()
            .get(fileId=drive_file_id, fields="parents")
            .execute()
        )
        old_parents = ",".join(meta.get("parents", []))

        body: dict[str, Any] = {}
        if app_properties:
            body["appProperties"] = app_properties

        request = self._service.files().update(
            fileId=drive_file_id,
            addParents=tombstones_id,
            removeParents=old_parents,
            body=body,
            fields="id,parents,appProperties",
        )
        _execute_with_retry(
            request,
            description=f"move_to_tombstones[{drive_file_id}]",
            not_found_file_id=drive_file_id,
        )
        logger.info(f"Drive tombstone 이동: {drive_file_id}")

    def ensure_tombstones_folder(self) -> str:
        """`.sync/tombstones/` 폴더 ID를 반환한다. 없으면 생성.

        세션 내 캐시하여 중복 생성을 방지한다.
        """
        if self._tombstones_folder_id is not None:
            return self._tombstones_folder_id

        # .sync 폴더 확보
        sync_folder_id = self.ensure_folder_path(".sync")

        # tombstones 하위 폴더 확인/생성
        tombstones_id = self.find_folder("tombstones", sync_folder_id)
        if not tombstones_id:
            tombstones_id = self.create_folder("tombstones", sync_folder_id)
            logger.info(f"Drive .sync/tombstones/ 폴더 생성: {tombstones_id}")

        self._tombstones_folder_id = tombstones_id
        return tombstones_id

    # ── convergence.json (PR4: tombstone GC 합의) ──────────────────────

    def _find_convergence_file(self) -> str | None:
        """.sync/convergence.json 파일 ID를 찾는다. 없으면 None."""
        sync_folder_id = self.ensure_folder_path(".sync")
        query = (
            f"'{sync_folder_id}' in parents "
            f"and name = 'convergence.json' "
            f"and trashed = false"
        )
        request = self._service.files().list(
            q=query, fields="files(id)", pageSize=1
        )
        result = _execute_with_retry(request, description="find_convergence_file")
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def _ensure_convergence_file(self) -> str:
        """.sync/convergence.json 파일 ID를 반환한다. 없으면 빈 파일 생성."""
        if self._convergence_file_id is not None:
            return self._convergence_file_id

        file_id = self._find_convergence_file()
        if file_id is None:
            sync_folder_id = self.ensure_folder_path(".sync")
            media = googleapiclient.http.MediaIoBaseUpload(
                io.BytesIO(b"{}"), mimetype="application/json"
            )
            meta = {
                "name": "convergence.json",
                "parents": [sync_folder_id],
                "mimeType": "application/json",
            }
            request = self._service.files().create(
                body=meta, media_body=media, fields="id"
            )
            result = _execute_with_retry(request, description="create_convergence_file")
            file_id = result["id"]
            logger.info(f"Drive .sync/convergence.json 생성: {file_id}")

        self._convergence_file_id = file_id
        return file_id

    def read_convergence(self) -> tuple[dict[str, Any], str]:
        """.sync/convergence.json 을 읽어 (data, version)을 반환한다.

        version은 Drive의 단조증가 `version` 필드를 optimistic etag로 사용.
        파일이 없으면 ({}, "0") 반환 (ConvergenceManager의 첫 write가 생성).
        """
        file_id = self._find_convergence_file()
        if file_id is None:
            return {}, "0"
        self._convergence_file_id = file_id

        media_req = self._service.files().get_media(fileId=file_id)
        content = _execute_with_retry(
            media_req, description=f"read_convergence[{file_id}]"
        )
        try:
            data = json.loads(content.decode("utf-8")) if content else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("convergence.json 파싱 실패, 빈 상태로 처리")
            data = {}

        meta_req = self._service.files().get(fileId=file_id, fields="version")
        meta = _execute_with_retry(
            meta_req, description=f"read_convergence_version[{file_id}]"
        )
        version = str(meta.get("version", "0"))
        return data, version

    def write_convergence(
        self, data: dict[str, Any], expected_version: str
    ) -> bool:
        """convergence.json 을 조건부로 덮어쓴다.

        expected_version이 현재 Drive version과 일치할 때만 write.
        Drive API v3가 If-Match 헤더를 노출하지 않으므로, write 직전
        version을 재조회하여 optimistic concurrency를 구현한다.

        반환값: 성공 시 True, 경합(expected 불일치) 시 False.
        """
        file_id = self._ensure_convergence_file()

        # 첫 생성이 아닌 경우, 쓰기 직전 version 재확인
        if expected_version != "0":
            meta_req = self._service.files().get(
                fileId=file_id, fields="version"
            )
            meta = _execute_with_retry(
                meta_req, description=f"write_convergence_check[{file_id}]"
            )
            current_version = str(meta.get("version", "0"))
            if current_version != expected_version:
                logger.info(
                    f"convergence.json etag mismatch: "
                    f"expected={expected_version}, got={current_version}"
                )
                return False

        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        media = googleapiclient.http.MediaIoBaseUpload(
            io.BytesIO(body), mimetype="application/json"
        )
        request = self._service.files().update(
            fileId=file_id, media_body=media, fields="id,version"
        )
        _execute_with_retry(
            request,
            description=f"write_convergence[{file_id}]",
            not_found_file_id=file_id,
        )
        return True

    def rename(self, drive_file_id: str, new_name: str) -> None:
        """Drive 파일의 이름을 변경한다."""
        request = self._service.files().update(
            fileId=drive_file_id, body={"name": new_name}
        )
        _execute_with_retry(
            request,
            description=f"rename[{drive_file_id}]",
            not_found_file_id=drive_file_id,
        )
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
        request = self._service.files().get(fileId=drive_file_id, fields=fields)
        return _execute_with_retry(
            request,
            description=f"get_file_metadata[{drive_file_id}]",
            not_found_file_id=drive_file_id,
        )

    # ── 폴더 조작 ─────────────────────────────────────────────────────────

    def find_folder(self, name: str, parent_id: str) -> str | None:
        """부모 폴더 아래에서 이름으로 폴더를 찾는다. 없으면 None."""
        safe_name = name.replace("'", "\\'")
        q = (
            f"name='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='{MIME_FOLDER}' and trashed=false"
        )
        request = (
            self._service.files()
            .list(q=q, fields="files(id)", pageSize=1)
        )
        resp = _execute_with_retry(
            request, description=f"find_folder[{name}]"
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
        request = self._service.files().create(body=meta, fields="id")
        result = _execute_with_retry(
            request, description=f"create_folder[{name}]"
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
                self._folder_id_to_rel[folder_id] = built
                self._vault_folder_ids.add(folder_id)

            parent_id = folder_id

        return parent_id

    def find_folder_path(self, rel_folder_path: str) -> str | None:
        """상대 경로의 폴더 ID를 조회. 존재하지 않으면 None (생성하지 않음)."""
        if not rel_folder_path or rel_folder_path == ".":
            return self._folder_id

        cached = self._folder_cache.get(rel_folder_path)
        if cached:
            return cached

        parent_id = self._folder_id
        built = ""
        for part in rel_folder_path.split("/"):
            child = self.find_folder(part, parent_id)
            if child is None:
                return None
            built = f"{built}/{part}".lstrip("/")
            self._folder_cache[built] = child
            self._folder_id_to_rel[child] = built
            self._vault_folder_ids.add(child)
            parent_id = child
        return parent_id

    def resolve_vault_rel_path(
        self,
        parents: list[str] | None,
        name: str,
    ) -> str | None:
        """파일의 parents 체인을 타고 볼트 루트까지 올라가 rel_path를 구성한다.

        Changes API는 경로 문자열을 주지 않고 부모 폴더 ID 배열(`parents`)만
        제공한다. 볼트 폴더 안의 파일이면 루트(self._folder_id)까지 도달하므로
        그 경로상의 폴더 이름을 연결해 rel_path를 만든다.

        반환:
            - 볼트 루트 바로 아래면 "name"
            - 서브폴더 안이면 "a/b/name"
            - 볼트 밖으로 판단되거나 해석 실패 시 None
        """
        if not parents or not name:
            return None

        # 단일 parent 기준으로 체인 추적. Drive에서 file.parents는 보통 1개.
        parent_id = parents[0]

        # 루트에 바로 붙어 있으면 끝
        if parent_id == self._folder_id:
            return name

        # non-vault로 확정된 폴더면 즉시 중단
        if parent_id in self._non_vault_ids:
            return None

        # 캐시 히트: 역매핑이 알고 있는 폴더
        cached_rel = self._folder_id_to_rel.get(parent_id)
        if cached_rel is not None:
            if cached_rel == "":
                return name
            return f"{cached_rel}/{name}"

        # 캐시 미스: parents 체인을 따라 루트까지 올라가며 경로 수집
        segments: list[str] = []
        path_ids: list[str] = []
        current_id = parent_id

        max_depth = 32  # 무한 루프 방어
        for _ in range(max_depth):
            # 체인 중간에서 캐시에 이미 있는 폴더를 만남 → 거기부터 연결
            known_rel = self._folder_id_to_rel.get(current_id)
            if known_rel is not None:
                rel_prefix = known_rel
                # 수집된 segments는 아래→위 순이므로 역순으로 합친다
                for i, seg in enumerate(reversed(segments)):
                    rel_prefix = f"{rel_prefix}/{seg}" if rel_prefix else seg
                    folder_id = path_ids[len(segments) - 1 - i]
                    self._folder_cache[rel_prefix] = folder_id
                    self._folder_id_to_rel[folder_id] = rel_prefix
                    self._vault_folder_ids.add(folder_id)
                return f"{rel_prefix}/{name}" if rel_prefix else name

            if current_id in self._non_vault_ids:
                # 경로 자체가 볼트 밖
                self._non_vault_ids.update(path_ids)
                return None

            # Drive API로 폴더 메타 조회
            try:
                request = self._service.files().get(
                    fileId=current_id, fields="id,name,parents"
                )
                meta = _execute_with_retry(
                    request, description=f"resolve_vault_rel_path[{current_id}]"
                )
            except DriveFileNotFoundError:
                self._non_vault_ids.update(path_ids + [current_id])
                return None
            except Exception:
                logger.debug(
                    f"resolve_vault_rel_path 폴더 조회 실패: {current_id}",
                    exc_info=True,
                )
                return None

            segments.append(meta.get("name", ""))
            path_ids.append(current_id)

            grand = meta.get("parents") or []
            if not grand:
                # My Drive 루트에 도달 → 볼트 밖
                self._non_vault_ids.update(path_ids)
                return None
            current_id = grand[0]

        # 깊이 제한 초과 → 안전 측 fallback
        logger.warning(
            f"resolve_vault_rel_path 깊이 제한 도달(parents={parents[0]}, name={name})"
        )
        return None

    def find_file_by_rel_path(self, rel_path: str) -> str | None:
        """상대 경로의 파일 ID를 조회. 없으면 None.

        Upload 직전 중복 생성 방지용 — state에 drive_id가 없을 때
        Drive에 이미 같은 경로 파일이 있는지 확인하여 재사용한다.
        """
        if "/" in rel_path:
            parent_rel, name = rel_path.rsplit("/", 1)
        else:
            parent_rel, name = "", rel_path

        parent_id = self.find_folder_path(parent_rel)
        if parent_id is None:
            return None

        safe_name = name.replace("'", "\\'")
        q = (
            f"name='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType!='{MIME_FOLDER}' and trashed=false"
        )
        request = self._service.files().list(q=q, fields="files(id)", pageSize=1)
        resp = _execute_with_retry(
            request, description=f"find_file_by_rel_path[{rel_path}]"
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

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
                    "size,md5Checksum,appProperties))"
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

        # tombstones 폴더로의 move 감지
        is_in_tombstones = (
            self._tombstones_folder_id is not None
            and self._tombstones_folder_id in parents
        )

        # 삭제 판정: removed=True OR trashed=True OR tombstones 폴더 이동
        is_deleted = removed or is_trashed or is_in_tombstones

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
            "app_properties": file_meta.get("appProperties"),
            "parents": parents,
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
                request = (
                    self._service.files()
                    .list(
                        q=q,
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,size,parents,md5Checksum,appProperties)",
                        pageSize=100,
                        pageToken=page_token,
                    )
                )
                resp = _execute_with_retry(
                    request, description=f"list_all_files[{current_folder_id}]"
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
                        self._folder_id_to_rel[item["id"]] = rel_path
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
