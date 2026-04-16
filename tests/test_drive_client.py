"""src/drive_client.py 단위 테스트.

Google Drive API 호출은 mock 처리한다.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from src.config import SyncConfig
from src.drive_client import (
    MIME_FOLDER,
    DriveClient,
    TokenInvalidError,
    _execute_with_retry,
)


@pytest.fixture
def mock_config(tmp_path):
    """테스트용 SyncConfig를 생성한다."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}", encoding="utf-8")
    token_file = tmp_path / "token.json"

    return SyncConfig(
        vault_path=tmp_path / "vault",
        drive_folder_id="root_folder_id",
        device_id="test_pc",
        credentials_file=creds_file,
        token_file=token_file,
    )


@pytest.fixture
def drive_client(mock_config):
    """mock 서비스가 주입된 DriveClient를 생성한다."""
    client = DriveClient(mock_config)
    client._service = MagicMock()
    return client


class TestAuthenticate:
    """DriveClient.authenticate 테스트."""

    def test_authenticate_with_existing_token(self, mock_config, tmp_path):
        """유효한 토큰 파일이 있으면 브라우저를 열지 않는다."""
        mock_creds = MagicMock()
        mock_creds.valid = True

        with patch(
            "src.drive_client.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ), patch("src.drive_client.googleapiclient.discovery.build") as mock_build:
            mock_config.token_file.write_text("{}", encoding="utf-8")
            client = DriveClient(mock_config)
            client.authenticate()

            mock_build.assert_called_once_with("drive", "v3", credentials=mock_creds)

    def test_authenticate_missing_credentials_raises(self, tmp_path):
        """credentials 파일이 없으면 FileNotFoundError."""
        config = SyncConfig(
            vault_path=tmp_path,
            drive_folder_id="id",
            device_id="pc",
            credentials_file=tmp_path / "nonexistent.json",
            token_file=tmp_path / "token.json",
        )
        client = DriveClient(config)

        with patch(
            "src.drive_client.Credentials.from_authorized_user_file",
            return_value=None,
        ):
            with pytest.raises(FileNotFoundError):
                client.authenticate()


class TestUpload:
    """DriveClient.upload 테스트."""

    def test_upload_new_file(self, drive_client, tmp_path):
        """existing_id=None이면 create를 호출한다."""
        test_file = tmp_path / "test.md"
        test_file.write_text("content", encoding="utf-8")

        mock_create = drive_client._service.files().create
        mock_create.return_value.execute.return_value = {"id": "new_file_id"}

        # find_folder가 None을 반환 → create_folder 호출
        mock_list = drive_client._service.files().list
        mock_list.return_value.execute.return_value = {"files": []}
        mock_create_folder = drive_client._service.files().create
        mock_create_folder.return_value.execute.return_value = {"id": "new_file_id"}

        result = drive_client.upload(test_file, "test.md")
        assert result == "new_file_id"

    def test_upload_existing_file(self, drive_client, tmp_path):
        """existing_id가 있으면 update를 호출한다."""
        test_file = tmp_path / "test.md"
        test_file.write_text("updated content", encoding="utf-8")

        mock_update = drive_client._service.files().update
        mock_update.return_value.execute.return_value = {}

        result = drive_client.upload(test_file, "test.md", existing_id="existing_123")
        assert result == "existing_123"


class TestDownload:
    """DriveClient.download 테스트."""

    def test_download_writes_file(self, drive_client, tmp_path):
        """다운로드한 내용이 로컬 파일에 기록된다."""
        local_path = tmp_path / "downloaded" / "note.md"
        content = b"downloaded content"

        mock_get_media = drive_client._service.files().get_media
        mock_get_media.return_value.execute.return_value = content

        drive_client.download("file_id_123", local_path)

        assert local_path.exists()
        assert local_path.read_bytes() == content

    def test_download_creates_parent_dirs(self, drive_client, tmp_path):
        """부모 디렉토리가 없으면 자동 생성한다."""
        local_path = tmp_path / "deep" / "nested" / "dir" / "file.md"

        mock_get_media = drive_client._service.files().get_media
        mock_get_media.return_value.execute.return_value = b"data"

        drive_client.download("file_id", local_path)
        assert local_path.parent.exists()


class TestDelete:
    """DriveClient.delete 테스트."""

    def test_delete_trashes_file(self, drive_client):
        """delete가 trashed=True로 업데이트한다."""
        drive_client.delete("file_id_123")

        drive_client._service.files().update.assert_called_with(
            fileId="file_id_123", body={"trashed": True}
        )


class TestRename:
    """DriveClient.rename 테스트."""

    def test_rename_file(self, drive_client):
        """파일 이름을 변경한다."""
        drive_client.rename("file_id_123", "new_name.md")

        drive_client._service.files().update.assert_called_with(
            fileId="file_id_123", body={"name": "new_name.md"}
        )


class TestMove:
    """DriveClient.move 테스트."""

    def test_move_file(self, drive_client):
        """파일을 새 폴더로 이동한다."""
        mock_get = drive_client._service.files().get
        mock_get.return_value.execute.return_value = {"parents": ["old_parent_id"]}

        drive_client.move("file_id", "new_parent_id", new_name="renamed.md")

        drive_client._service.files().update.assert_called_with(
            fileId="file_id",
            addParents="new_parent_id",
            removeParents="old_parent_id",
            body={"name": "renamed.md"},
            fields="id,parents",
        )


class TestEnsureFolderPath:
    """DriveClient.ensure_folder_path 테스트."""

    def test_empty_path_returns_root(self, drive_client):
        """빈 경로면 루트 폴더 ID를 반환한다."""
        assert drive_client.ensure_folder_path("") == "root_folder_id"
        assert drive_client.ensure_folder_path(".") == "root_folder_id"

    def test_cached_folder(self, drive_client):
        """캐시에 있는 폴더는 API를 호출하지 않는다."""
        drive_client._folder_cache["notes"] = "cached_folder_id"
        result = drive_client.ensure_folder_path("notes")
        assert result == "cached_folder_id"

    def test_creates_nested_folders(self, drive_client):
        """중첩된 폴더 경로를 생성한다."""
        # find_folder가 항상 None → create_folder 필요
        mock_list = drive_client._service.files().list
        mock_list.return_value.execute.return_value = {"files": []}

        mock_create = drive_client._service.files().create
        mock_create.return_value.execute.side_effect = [
            {"id": "notes_id"},
            {"id": "archive_id"},
        ]

        result = drive_client.ensure_folder_path("notes/archive")
        assert result == "archive_id"
        assert drive_client._folder_cache["notes"] == "notes_id"
        assert drive_client._folder_cache["notes/archive"] == "archive_id"


class TestGetChanges:
    """DriveClient.get_changes 테스트."""

    def test_single_page_changes(self, drive_client):
        """단일 페이지 변경 목록을 처리한다."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "new_token_456",
            "changes": [
                {
                    "fileId": "file1",
                    "removed": False,
                    "file": {
                        "id": "file1",
                        "name": "note.md",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": False,
                        "size": "100",
                    },
                },
            ],
        }

        changes, new_token = drive_client.get_changes("old_token_123")
        assert new_token == "new_token_456"
        assert len(changes) == 1
        assert changes[0]["file_id"] == "file1"
        assert changes[0]["removed"] is False

    def test_trashed_file_marked_as_removed(self, drive_client):
        """trashed=True인 파일은 removed=True로 정규화된다."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {
                    "fileId": "file1",
                    "removed": False,
                    "file": {
                        "id": "file1",
                        "name": "deleted.md",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": True,
                        "size": "100",
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        assert len(changes) == 1
        assert changes[0]["removed"] is True
        assert changes[0]["file"] is None

    def test_folder_changes_excluded(self, drive_client):
        """폴더 변경은 파일 목록에서 제외된다."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {
                    "fileId": "folder1",
                    "removed": False,
                    "file": {
                        "id": "folder1",
                        "name": "new_folder",
                        "mimeType": MIME_FOLDER,
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": False,
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        assert len(changes) == 0

    def test_changes_outside_vault_excluded(self, drive_client):
        """볼트 폴더 밖의 변경은 제외된다."""
        # _non_vault_ids에 부모 ID 등록하여 볼트 밖으로 판정
        drive_client._non_vault_ids.add("other_folder_id")

        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {
                    "fileId": "file_outside",
                    "removed": False,
                    "file": {
                        "id": "file_outside",
                        "name": "other.md",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["other_folder_id"],
                        "trashed": False,
                        "size": "50",
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        assert len(changes) == 0

    def test_multi_page_changes(self, drive_client):
        """멀티페이지 응답을 올바르게 처리한다."""
        call_count = [0]

        def mock_list_execute():
            call_count[0] += 1
            if call_count[0] == 1:
                # 첫 페이지: nextPageToken 있음
                return {
                    "nextPageToken": "page2_token",
                    "changes": [
                        {
                            "fileId": "f1",
                            "removed": False,
                            "file": {
                                "id": "f1",
                                "name": "page1.md",
                                "mimeType": "text/plain",
                                "modifiedTime": "2026-04-14T10:00:00Z",
                                "parents": ["root_folder_id"],
                                "trashed": False,
                                "size": "100",
                            },
                        },
                    ],
                }
            else:
                # 두 번째 페이지: newStartPageToken으로 종료
                return {
                    "newStartPageToken": "final_token",
                    "changes": [
                        {
                            "fileId": "f2",
                            "removed": False,
                            "file": {
                                "id": "f2",
                                "name": "page2.md",
                                "mimeType": "text/plain",
                                "modifiedTime": "2026-04-14T11:00:00Z",
                                "parents": ["root_folder_id"],
                                "trashed": False,
                                "size": "200",
                            },
                        },
                    ],
                }

        drive_client._service.changes().list.return_value.execute = mock_list_execute

        changes, new_token = drive_client.get_changes("start_token")
        assert len(changes) == 2
        assert new_token == "final_token"
        assert changes[0]["file_id"] == "f1"
        assert changes[1]["file_id"] == "f2"

    def test_removed_without_meta_vault_file(self, drive_client):
        """removed=True + 메타 없음: 볼트 파일(캐시에 존재)이면 반환한다."""
        drive_client._vault_folder_ids.add("known_vault_file")

        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {"fileId": "known_vault_file", "removed": True},
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        assert len(changes) == 1
        assert changes[0]["removed"] is True
        assert changes[0]["file"] is None

    def test_removed_without_meta_non_vault_file(self, drive_client):
        """removed=True + 메타 없음: 볼트 밖 파일이면 무시한다."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {"fileId": "unknown_file_id", "removed": True},
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        assert len(changes) == 0

    def test_new_vault_folder_registered(self, drive_client):
        """볼트 안의 새 폴더 변경 시 _vault_folder_ids에 등록된다."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "token2",
            "changes": [
                {
                    "fileId": "new_subfolder",
                    "removed": False,
                    "file": {
                        "id": "new_subfolder",
                        "name": "archive",
                        "mimeType": MIME_FOLDER,
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": False,
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("token1")
        # 폴더 변경은 파일 목록에서 제외
        assert len(changes) == 0
        # 하지만 _vault_folder_ids에는 등록됨
        assert "new_subfolder" in drive_client._vault_folder_ids


class TestIsInVault:
    """DriveClient._is_in_vault 테스트."""

    def test_direct_child_of_root(self, drive_client):
        """루트 폴더의 직계 자식은 볼트 파일이다."""
        assert drive_client._is_in_vault("file1", ["root_folder_id"]) is True

    def test_child_of_cached_folder(self, drive_client):
        """캐시된 폴더의 자식은 볼트 파일이다."""
        drive_client._folder_cache["notes"] = "notes_folder_id"
        drive_client._vault_folder_ids.add("notes_folder_id")
        assert drive_client._is_in_vault("file1", ["notes_folder_id"]) is True

    def test_child_of_vault_folder_ids(self, drive_client):
        """_vault_folder_ids에 등록된 폴더의 자식은 볼트 파일이다."""
        drive_client._vault_folder_ids.add("deep_folder_id")
        assert drive_client._is_in_vault("file1", ["deep_folder_id"]) is True

    def test_non_vault_folder(self, drive_client):
        """볼트 밖으로 확인된 폴더의 자식은 볼트 파일이 아니다."""
        drive_client._non_vault_ids.add("external_id")
        assert drive_client._is_in_vault("file1", ["external_id"]) is False

    def test_empty_parents(self, drive_client):
        """parents가 비어있으면 볼트 파일이 아니다."""
        assert drive_client._is_in_vault("file1", []) is False


class TestIsUnderVault:
    """DriveClient._is_under_vault 테스트."""

    def test_direct_child_of_root(self, drive_client):
        """볼트 루트의 직계 자식 폴더는 볼트 하위이다."""
        drive_client._service.files().get.return_value.execute.return_value = {
            "id": "sub_folder", "name": "notes", "parents": ["root_folder_id"]
        }
        assert drive_client._is_under_vault("sub_folder") is True
        assert "sub_folder" in drive_client._vault_folder_ids

    def test_nested_folder_caches_all_path_ids(self, drive_client):
        """재귀 탐색 성공 시 경로상 모든 폴더 ID를 _vault_folder_ids에 등록한다."""
        call_count = [0]

        def mock_get_execute():
            call_count[0] += 1
            if call_count[0] == 1:
                # deep_folder의 부모 → mid_folder
                return {"id": "deep_folder", "name": "deep", "parents": ["mid_folder"]}
            else:
                # mid_folder의 부모 → root
                return {"id": "mid_folder", "name": "mid", "parents": ["root_folder_id"]}

        drive_client._service.files().get.return_value.execute = mock_get_execute

        assert drive_client._is_under_vault("deep_folder") is True
        assert "deep_folder" in drive_client._vault_folder_ids
        assert "mid_folder" in drive_client._vault_folder_ids

    def test_non_vault_folder_caches_to_non_vault(self, drive_client):
        """볼트 밖 폴더는 _non_vault_ids에 캐싱된다."""
        # parents가 비어있으면 My Drive 루트 → 볼트 밖
        drive_client._service.files().get.return_value.execute.return_value = {
            "id": "external", "name": "external", "parents": []
        }
        assert drive_client._is_under_vault("external") is False
        assert "external" in drive_client._non_vault_ids

    def test_already_known_vault_folder(self, drive_client):
        """이미 _vault_folder_ids에 등록된 폴더는 API 호출 없이 True."""
        drive_client._vault_folder_ids.add("known_folder")
        assert drive_client._is_under_vault("known_folder") is True

    def test_api_failure_treats_as_non_vault(self, drive_client):
        """API 호출 실패 시 볼트 밖으로 간주한다."""
        drive_client._service.files().get.return_value.execute.side_effect = Exception("API error")
        assert drive_client._is_under_vault("broken_folder") is False
        assert "broken_folder" in drive_client._non_vault_ids


class TestListAllFiles:
    """DriveClient.list_all_files 테스트."""

    def test_list_flat_files(self, drive_client):
        """루트 폴더의 플랫 파일 목록을 가져온다."""
        drive_client._service.files().list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "f1",
                    "name": "note1.md",
                    "mimeType": "text/plain",
                    "modifiedTime": "2026-04-14T10:00:00Z",
                    "size": "100",
                    "parents": ["root_folder_id"],
                },
                {
                    "id": "f2",
                    "name": "note2.md",
                    "mimeType": "text/plain",
                    "modifiedTime": "2026-04-14T11:00:00Z",
                    "size": "200",
                    "parents": ["root_folder_id"],
                },
            ],
        }

        files = drive_client.list_all_files()
        assert len(files) == 2
        assert files[0]["relative_path"] == "note1.md"
        assert files[1]["relative_path"] == "note2.md"

    def test_list_nested_files(self, drive_client):
        """중첩된 폴더의 파일도 탐색한다."""
        # 첫 호출: 루트 폴더 → 폴더 + 파일
        # 두 번째 호출: 하위 폴더 → 파일
        call_count = [0]

        def mock_execute():
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "files": [
                        {
                            "id": "folder1",
                            "name": "notes",
                            "mimeType": MIME_FOLDER,
                            "modifiedTime": "2026-04-14T10:00:00Z",
                            "parents": ["root_folder_id"],
                        },
                        {
                            "id": "f1",
                            "name": "root.md",
                            "mimeType": "text/plain",
                            "modifiedTime": "2026-04-14T10:00:00Z",
                            "size": "100",
                            "parents": ["root_folder_id"],
                        },
                    ],
                }
            else:
                return {
                    "files": [
                        {
                            "id": "f2",
                            "name": "nested.md",
                            "mimeType": "text/plain",
                            "modifiedTime": "2026-04-14T11:00:00Z",
                            "size": "200",
                            "parents": ["folder1"],
                        },
                    ],
                }

        drive_client._service.files().list.return_value.execute = mock_execute

        files = drive_client.list_all_files()
        assert len(files) == 2

        paths = {f["relative_path"] for f in files}
        assert "root.md" in paths
        assert "notes/nested.md" in paths

        # 폴더 캐시에 등록되었는지 확인
        assert drive_client._folder_cache["notes"] == "folder1"


class TestGetInitialToken:
    """DriveClient.get_initial_token 테스트."""

    def test_returns_token(self, drive_client):
        """시작 토큰을 반환한다."""
        drive_client._service.changes().getStartPageToken.return_value.execute.return_value = {
            "startPageToken": "initial_token_789"
        }

        token = drive_client.get_initial_token()
        assert token == "initial_token_789"


def _make_http_error(status: int, reason: str = "") -> HttpError:
    """googleapiclient HttpError를 간편 생성한다."""
    resp = MagicMock()
    resp.status = status
    resp.reason = reason or "test"
    return HttpError(resp, b'{"error":{"message":"mock"}}')


class TestExecuteWithRetry:
    """_execute_with_retry 정책 검증."""

    def test_success_no_retry(self):
        """성공 응답이면 재시도하지 않는다."""
        request = MagicMock()
        request.execute.return_value = {"ok": True}

        with patch("src.drive_client.time.sleep") as mock_sleep:
            result = _execute_with_retry(request)

        assert result == {"ok": True}
        assert request.execute.call_count == 1
        mock_sleep.assert_not_called()

    def test_429_exponential_backoff(self):
        """429 응답 mock 시 지수백오프 대기시간이 1→2→4…로 증가."""
        err_429 = _make_http_error(429, "Too Many Requests")

        request = MagicMock()
        # 3번 429, 4번째 성공
        request.execute.side_effect = [
            err_429, err_429, err_429,
            {"ok": True},
        ]

        with patch("src.drive_client.time.sleep") as mock_sleep:
            result = _execute_with_retry(request)

        assert result == {"ok": True}
        # 지수 증가 확인: 1 → 2 → 4
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert waits == [1.0, 2.0, 4.0]

    def test_429_cap_at_max_delay(self):
        """429가 반복되면 RATE_LIMIT_MAX_DELAY(300s)를 넘지 않는다."""
        from src.drive_client import RATE_LIMIT_MAX_DELAY

        err_429 = _make_http_error(429)
        request = MagicMock()
        # 10번 429 + 성공
        request.execute.side_effect = [err_429] * 10 + [{"ok": True}]

        with patch("src.drive_client.time.sleep") as mock_sleep:
            result = _execute_with_retry(request)

        assert result == {"ok": True}
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert all(w <= RATE_LIMIT_MAX_DELAY for w in waits)
        # 끝부분은 cap에 도달
        assert waits[-1] == RATE_LIMIT_MAX_DELAY

    def test_410_raises_token_invalid(self):
        """410 Gone 수신 시 TokenInvalidError로 변환된다."""
        err_410 = _make_http_error(410, "Gone")
        request = MagicMock()
        request.execute.side_effect = err_410

        with patch("src.drive_client.time.sleep"):
            with pytest.raises(TokenInvalidError):
                _execute_with_retry(request)

    def test_401_raises_immediately(self):
        """401 Unauthorized는 재시도 없이 즉시 전파."""
        err_401 = _make_http_error(401, "Unauthorized")
        request = MagicMock()
        request.execute.side_effect = err_401

        with patch("src.drive_client.time.sleep") as mock_sleep:
            with pytest.raises(HttpError):
                _execute_with_retry(request)

        assert request.execute.call_count == 1
        mock_sleep.assert_not_called()

    def test_403_raises_immediately(self):
        """403 Forbidden도 즉시 전파."""
        err_403 = _make_http_error(403, "Forbidden")
        request = MagicMock()
        request.execute.side_effect = err_403

        with patch("src.drive_client.time.sleep") as mock_sleep:
            with pytest.raises(HttpError):
                _execute_with_retry(request)

        assert request.execute.call_count == 1
        mock_sleep.assert_not_called()

    def test_5xx_retries_then_raises(self):
        """5xx는 최대 3회 재시도 후 전파."""
        err_503 = _make_http_error(503, "Service Unavailable")
        request = MagicMock()
        request.execute.side_effect = err_503  # 무한 503

        with patch("src.drive_client.time.sleep") as mock_sleep:
            with pytest.raises(HttpError):
                _execute_with_retry(request)

        # 최초 1회 + 3회 재시도 = 4회 호출
        assert request.execute.call_count == 4
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert waits == [1.0, 2.0, 4.0]

    def test_network_error_retries(self):
        """네트워크 오류도 3회 재시도 (1→2→4s)."""
        request = MagicMock()
        request.execute.side_effect = [
            OSError("connection reset"),
            OSError("timeout"),
            {"ok": True},
        ]

        with patch("src.drive_client.time.sleep") as mock_sleep:
            result = _execute_with_retry(request)

        assert result == {"ok": True}
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert waits == [1.0, 2.0]


class TestGetChangesErrorHandling:
    """get_changes의 오류 처리 경로 검증."""

    def test_get_changes_410_propagates_token_invalid(self, drive_client):
        """get_changes가 410 Gone을 받으면 TokenInvalidError를 던져
        상위에서 토큰 재발급 + run_without_state 경로를 유도하게 한다."""
        err_410 = _make_http_error(410, "Gone")
        drive_client._service.changes().list.return_value.execute.side_effect = err_410

        with patch("src.drive_client.time.sleep"):
            with pytest.raises(TokenInvalidError):
                drive_client.get_changes("stale_token")

    def test_get_changes_429_backoff_then_success(self, drive_client):
        """get_changes 429 → 재시도 → 성공."""
        err_429 = _make_http_error(429)
        success = {
            "newStartPageToken": "new_token",
            "changes": [],
        }
        drive_client._service.changes().list.return_value.execute.side_effect = [
            err_429, err_429, success,
        ]

        with patch("src.drive_client.time.sleep") as mock_sleep:
            changes, new_token = drive_client.get_changes("old_token")

        assert new_token == "new_token"
        assert changes == []
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert waits == [1.0, 2.0]


class TestGetChangesSchemaNormalization:
    """get_changes 반환 스키마 정규화 검증."""

    def test_file_payload_has_name_modified_time_md5(self, drive_client):
        """반환된 file에 name, modified_time, md5만 포함되도록 정규화."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "tok",
            "changes": [
                {
                    "fileId": "f1",
                    "removed": False,
                    "file": {
                        "id": "f1",
                        "name": "note.md",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": False,
                        "size": "100",
                        "md5Checksum": "abc123",
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("t")
        payload = changes[0]["file"]
        assert payload is not None
        assert payload["name"] == "note.md"
        assert payload["modified_time"] == "2026-04-14T10:00:00Z"
        assert payload["md5"] == "abc123"
        assert set(payload.keys()) == {"name", "modified_time", "md5"}

    def test_google_doc_without_md5(self, drive_client):
        """md5Checksum이 없는 Google Doc은 md5=None으로 정규화."""
        drive_client._service.changes().list.return_value.execute.return_value = {
            "newStartPageToken": "tok",
            "changes": [
                {
                    "fileId": "gdoc1",
                    "removed": False,
                    "file": {
                        "id": "gdoc1",
                        "name": "문서",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-04-14T10:00:00Z",
                        "parents": ["root_folder_id"],
                        "trashed": False,
                    },
                },
            ],
        }

        changes, _ = drive_client.get_changes("t")
        assert changes[0]["file"]["md5"] is None
