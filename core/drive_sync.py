from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import googleapiclient.discovery
import googleapiclient.http
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
MIME_FOLDER = "application/vnd.google-apps.folder"


def _parse_rfc3339(timestamp: str) -> float:
    """Parse Google Drive's RFC3339 timestamp to Unix epoch float."""
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return dt.timestamp()


class DriveSync:
    """
    Wraps Google Drive API v3 for bidirectional vault synchronization.

    Key concepts:
    - vault_root: absolute local Path of the Obsidian vault
    - drive_root_id: Drive folder ID (from config) that mirrors vault_root
    - _id_cache: dict mapping relative POSIX path → Drive file ID
      e.g., {"notes/foo.md": "1aBcD...", "notes": "1FolD..."}
    - ignore_paths / ignore_lock: shared with DebouncedHandler to suppress
      circular watchdog events when writing downloaded files to disk
    """

    def __init__(self, config: dict, vault_root: Path):
        self._config = config
        self.vault_root = vault_root

        drive_cfg = config["drive"]
        self._credentials_file = Path(drive_cfg["credentials_file"])
        self._token_file = Path(drive_cfg["token_file"])
        self._drive_root_id: str = drive_cfg["folder_id"]
        self._poll_interval: int = config["sync"]["poll_interval_seconds"]
        self._delete_local: bool = config.get("sync", {}).get("delete_local", False)
        self._cache_file = Path("drive_id_cache.json")

        # Circular sync prevention — shared with DebouncedHandler by reference
        self.ignore_paths: set[str] = set()
        self.ignore_lock = threading.Lock()

        self._service = None
        self._id_cache: dict[str, str] = {}  # rel_posix_path → drive_id
        self._cache_lock = threading.Lock()
        self._upload_lock = threading.Lock()  # 동일 파일 중복 업로드 방지

        self._debounce_seconds: float = config.get("sync", {}).get("debounce_seconds", 5)
        self._page_token: str | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Authentication ────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """
        OAuth2 flow for an installed (desktop) app.
        First run: opens browser for consent, saves token.json.
        Subsequent runs: loads token.json, auto-refreshes if expired.
        """
        creds: Credentials | None = None

        if self._token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_file), SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                logger.info("OAuth token refreshed")
            else:
                if not self._credentials_file.exists():
                    raise FileNotFoundError(
                        f"credentials.json not found at '{self._credentials_file}'. "
                        "Download it from Google Cloud Console → APIs & Services → "
                        "Credentials → OAuth 2.0 Client IDs (Desktop app)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_file), SCOPES
                )
                creds = flow.run_local_server(port=0)
                logger.info("OAuth consent completed")

            self._token_file.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Token saved to %s", self._token_file)

        self._service = googleapiclient.discovery.build(
            "drive", "v3", credentials=creds
        )
        logger.info("Google Drive service ready")

    # ── ID Cache ──────────────────────────────────────────────────────────

    def _rel(self, local_path: Path) -> str:
        """Convert absolute local path to POSIX relative path (cache key)."""
        return local_path.relative_to(self.vault_root).as_posix()

    def _load_cache(self) -> None:
        if self._cache_file.exists():
            try:
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                with self._cache_lock:
                    self._id_cache = data
                logger.debug("Loaded %d cached Drive IDs", len(self._id_cache))
            except Exception:
                logger.warning(
                    "Failed to load drive_id_cache.json, starting fresh"
                )

    def _save_cache(self) -> None:
        try:
            with self._cache_lock:
                data = dict(self._id_cache)
            self._cache_file.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.warning("Failed to save drive_id_cache.json")

    def _cache_get(self, rel_path: str) -> str | None:
        with self._cache_lock:
            return self._id_cache.get(rel_path)

    def _cache_set(self, rel_path: str, drive_id: str) -> None:
        with self._cache_lock:
            self._id_cache[rel_path] = drive_id
        self._save_cache()

    def _cache_delete(self, rel_path: str) -> None:
        with self._cache_lock:
            self._id_cache.pop(rel_path, None)
        self._save_cache()

    # ── Folder helpers ────────────────────────────────────────────────────

    def _get_or_create_folder(self, rel_folder_path: str) -> str:
        """
        Recursively ensure a folder hierarchy exists in Drive.
        Returns the Drive folder ID of the deepest folder.

        Example: "notes/archive" → ensures notes/ then notes/archive/,
        returning the ID of notes/archive.
        """
        if not rel_folder_path or rel_folder_path == ".":
            return self._drive_root_id

        cached = self._cache_get(rel_folder_path)
        if cached:
            return cached

        parts = rel_folder_path.split("/")
        parent_id = self._drive_root_id
        built = ""

        for part in parts:
            built = f"{built}/{part}".lstrip("/")
            folder_id = self._cache_get(built)

            if not folder_id:
                safe_name = part.replace("'", "\\'")
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

                if files:
                    folder_id = files[0]["id"]
                else:
                    meta = {
                        "name": part,
                        "mimeType": MIME_FOLDER,
                        "parents": [parent_id],
                    }
                    folder_id = (
                        self._service.files()
                        .create(body=meta, fields="id")
                        .execute()["id"]
                    )
                    logger.info("Created Drive folder: %s", built)

                self._cache_set(built, folder_id)

            parent_id = folder_id

        return parent_id

    def ensure_folder(self, local_path: Path) -> str:
        """Public API used by SyncHook for directory-created events."""
        rel = self._rel(local_path)
        return self._get_or_create_folder(rel)

    # ── Upload / Delete / Move ────────────────────────────────────────────

    def upload_file(self, local_path: Path) -> None:
        """Upload or update a file on Drive. Handles both create and update."""
        if not local_path.exists():
            logger.warning("Upload skipped, file gone: %s", local_path)
            return

        # 업로드 락: 동일 파일에 대해 여러 스레드가 동시에 create() 하는 것을 방지
        with self._upload_lock:
            self._upload_file_locked(local_path)

    def _upload_file_locked(self, local_path: Path) -> None:
        rel = self._rel(local_path)
        parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
        parent_id = self._get_or_create_folder(parent_rel)

        media = googleapiclient.http.MediaFileUpload(
            str(local_path), resumable=False
        )
        existing_id = self._cache_get(rel)

        if existing_id:
            # last-write-wins: skip upload if Drive copy is newer
            try:
                drive_meta = (
                    self._service.files()
                    .get(fileId=existing_id, fields="modifiedTime")
                    .execute()
                )
                drive_mtime = _parse_rfc3339(drive_meta["modifiedTime"])
                local_mtime = local_path.stat().st_mtime
                if drive_mtime > local_mtime:
                    logger.debug(
                        "Skipping upload (Drive newer): %s", rel
                    )
                    return
            except Exception:
                logger.debug(
                    "Could not fetch Drive modifiedTime for %s, uploading anyway",
                    rel,
                )

            self._service.files().update(
                fileId=existing_id,
                body={"name": local_path.name},
                media_body=media,
            ).execute()
            logger.info("Updated Drive: %s", rel)

        else:
            # Cache miss: query Drive in case the file already exists there
            safe_name = local_path.name.replace("'", "\\'")
            q = (
                f"name='{safe_name}' and '{parent_id}' in parents "
                f"and trashed=false"
            )
            resp = (
                self._service.files()
                .list(q=q, fields="files(id)", pageSize=1)
                .execute()
            )
            files = resp.get("files", [])

            if files:
                existing_id = files[0]["id"]
                self._cache_set(rel, existing_id)
                self._service.files().update(
                    fileId=existing_id,
                    body={"name": local_path.name},
                    media_body=media,
                ).execute()
                logger.info("Updated Drive (cache miss): %s", rel)
            else:
                meta = {"name": local_path.name, "parents": [parent_id]}
                result = (
                    self._service.files()
                    .create(body=meta, media_body=media, fields="id")
                    .execute()
                )
                self._cache_set(rel, result["id"])
                logger.info("Uploaded to Drive: %s", rel)

    def delete_file(self, local_path: Path) -> None:
        """Trash a file on Drive (soft delete)."""
        rel = self._rel(local_path)
        file_id = self._cache_get(rel)

        if not file_id:
            logger.debug("Delete skipped (no Drive ID cached): %s", rel)
            return

        self._service.files().update(
            fileId=file_id, body={"trashed": True}
        ).execute()
        self._cache_delete(rel)
        logger.info("Trashed on Drive: %s", rel)

    def move_file(self, src_path: Path, dest_path: Path) -> None:
        """
        Rename or move a file on Drive.
        Handles rename-in-place and move-to-new-folder cases.
        """
        src_rel = self._rel(src_path)
        dest_rel = self._rel(dest_path)
        file_id = self._cache_get(src_rel)

        if not file_id:
            # No cached ID — treat as a fresh upload at the destination
            self.upload_file(dest_path)
            return

        dest_parent_rel = dest_rel.rsplit("/", 1)[0] if "/" in dest_rel else ""
        new_parent_id = self._get_or_create_folder(dest_parent_rel)

        meta = (
            self._service.files()
            .get(fileId=file_id, fields="parents")
            .execute()
        )
        old_parents = ",".join(meta.get("parents", []))

        self._service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parents,
            body={"name": dest_path.name},
            fields="id,parents",
        ).execute()

        self._cache_delete(src_rel)
        self._cache_set(dest_rel, file_id)
        logger.info("Moved on Drive: %s → %s", src_rel, dest_rel)

    # ── Drive→Local polling ───────────────────────────────────────────────

    def start_polling(self) -> None:
        """
        Initialize the Changes API page token (marks 'now') and
        start the background polling thread.
        """
        self._load_cache()

        result = self._service.changes().getStartPageToken().execute()
        self._page_token = result["startPageToken"]
        logger.info(
            "Drive polling initialized (start token: %s)", self._page_token
        )

        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="DrivePoller"
        )
        self._poll_thread.start()

    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=10)

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval):
            try:
                self._process_drive_changes()
            except Exception:
                logger.exception("Drive poll cycle failed")

    def _process_drive_changes(self) -> None:
        """
        Fetch all pending changes from Drive and apply them locally.
        Handles multi-page responses from the Changes API.
        """
        page_token = self._page_token

        while page_token:
            resp = (
                self._service.changes()
                .list(
                    pageToken=page_token,
                    spaces="drive",
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,"
                        "file(id,name,mimeType,modifiedTime,parents,trashed))"
                    ),
                    includeRemoved=True,
                    pageSize=100,
                )
                .execute()
            )

            for change in resp.get("changes", []):
                try:
                    self._apply_drive_change(change)
                except Exception:
                    logger.exception(
                        "Failed to apply Drive change: %s", change.get("fileId")
                    )

            if "newStartPageToken" in resp:
                self._page_token = resp["newStartPageToken"]
                page_token = None
            else:
                page_token = resp.get("nextPageToken")

    def _apply_drive_change(self, change: dict) -> None:
        """
        Translate one Drive change into a local filesystem operation.

        Skips:
        - Files outside the configured vault Drive folder
        - Folders (created lazily when a file inside them is downloaded)
        - Files removed/trashed: deleted locally only when sync.delete_local=true
        """
        file_meta = change.get("file")
        if not file_meta:
            return  # permanently deleted from trash — no metadata to act on

        if file_meta.get("mimeType") == MIME_FOLDER:
            return  # folders are created lazily on file download

        # Root check first — ignore changes outside our vault folder
        parents = file_meta.get("parents", [])
        if not self._is_under_root(change["fileId"], parents):
            return

        if file_meta.get("trashed") or change.get("removed"):
            if self._delete_local:
                self._apply_remote_delete(change["fileId"], file_meta)
            else:
                logger.info(
                    "Drive file trashed/removed (local copy kept, "
                    "set sync.delete_local=true to mirror deletions): %s",
                    file_meta.get("name"),
                )
            return

        local_path = self._drive_id_to_local_path(change["fileId"], file_meta)
        if not local_path:
            logger.warning(
                "Cannot map Drive file to local path: %s (%s)",
                file_meta["name"],
                change["fileId"],
            )
            return

        # last-write-wins conflict check
        drive_mtime = _parse_rfc3339(file_meta["modifiedTime"])
        if local_path.exists():
            local_mtime = local_path.stat().st_mtime
            if local_mtime >= drive_mtime:
                logger.debug(
                    "Skipping download (local newer or equal): %s", local_path
                )
                return

        self._download_file(change["fileId"], local_path)

    def _apply_remote_delete(self, file_id: str, file_meta: dict) -> None:
        """
        Delete the local file that corresponds to a trashed/removed Drive file.
        Only called when sync.delete_local=true.
        """
        local_path = self._drive_id_to_local_path(file_id, file_meta)
        if not local_path:
            logger.debug(
                "Remote delete: cannot map to local path (%s)", file_meta.get("name")
            )
            return

        if local_path.exists():
            path_str = str(local_path)
            with self.ignore_lock:
                self.ignore_paths.add(path_str)
            try:
                local_path.unlink()
                logger.info("Deleted local (Drive trashed): %s", local_path)
            except Exception:
                logger.exception("Failed to delete local file: %s", local_path)
            finally:
                def _remove_ignore(p: str) -> None:
                    time.sleep(self._debounce_seconds + 1)
                    with self.ignore_lock:
                        self.ignore_paths.discard(p)

                threading.Thread(
                    target=_remove_ignore, args=(path_str,), daemon=True
                ).start()

        rel = self._rel(local_path)
        self._cache_delete(rel)

    def _download_file(self, file_id: str, local_path: Path) -> None:
        """
        Download a file from Drive to local_path.

        Circular prevention:
        1. Add local_path to ignore_paths BEFORE writing to disk
        2. Write the file
        3. Remove from ignore_paths after debounce+1초 (디바운스보다 길어야 재업로드 방지)
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(local_path)

        with self.ignore_lock:
            self.ignore_paths.add(path_str)

        try:
            request = self._service.files().get_media(fileId=file_id)
            content = request.execute()
            local_path.write_bytes(content)
            logger.info("Downloaded from Drive: %s", local_path)
        except Exception:
            logger.exception("Download failed: %s", local_path)
        finally:
            def _remove_ignore(p: str) -> None:
                time.sleep(self._debounce_seconds + 1)
                with self.ignore_lock:
                    self.ignore_paths.discard(p)

            threading.Thread(
                target=_remove_ignore, args=(path_str,), daemon=True
            ).start()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _is_under_root(self, file_id: str, parents: list[str]) -> bool:
        """
        Heuristic check: is this Drive file inside our configured root folder?
        Checks direct parent or any cached folder ID (all cached folders are
        descendants of our root).
        """
        if self._drive_root_id in parents:
            return True
        with self._cache_lock:
            cached_ids = set(self._id_cache.values())
        return bool(set(parents) & cached_ids)

    def _drive_id_to_local_path(
        self, file_id: str, file_meta: dict
    ) -> Path | None:
        """
        Reverse-lookup: given a Drive file_id + metadata, find the local Path.
        Uses reverse scan of the ID cache, then tries to reconstruct from
        parent folder mapping.
        """
        # Reverse cache scan
        with self._cache_lock:
            for rel_path, cached_id in self._id_cache.items():
                if cached_id == file_id:
                    return self.vault_root / rel_path

        # Cache miss: find parent folder in cache
        parents = file_meta.get("parents", [])
        with self._cache_lock:
            cache_snapshot = dict(self._id_cache)

        for parent_id in parents:
            if parent_id == self._drive_root_id:
                local_path = self.vault_root / file_meta["name"]
                self._cache_set(file_meta["name"], file_id)
                return local_path

            for rel_path, cached_id in cache_snapshot.items():
                if cached_id == parent_id:
                    rel_file = (Path(rel_path) / file_meta["name"]).as_posix()
                    local_path = self.vault_root / rel_file
                    self._cache_set(rel_file, file_id)
                    return local_path

        return None
