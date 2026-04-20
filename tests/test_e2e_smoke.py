"""end-to-end smoke 테스트 (mock Drive + 임시 볼트).

실제 `main.run_app()`을 asyncio로 구동하고 mock `DriveClient`를 주입하여
기동 → 대조 → 종료 플로우를 시나리오별로 검증한다.

시나리오:
- S1 cold start: state 없음 → run_without_state로 Drive 파일 전체 다운로드.
- S2 warm start: 기존 state + 신규 로컬 파일 → upload action 발행.
- S3 watcher + poller 교차 이벤트: engine 잠금으로 직렬 처리, 손실 0.
- S4 TokenInvalidError 주입: TokenRefreshCoordinator 재진입 플로우.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import SyncConfig
from src.conflict import ConflictResolver
from src.drive_client import TokenInvalidError
from src.local_watcher import LocalWatcher
from src.main import AppContext, TokenRefreshCoordinator, run_app
from src.poller import AdaptivePoller
from src.reconciler import Reconciler
from src.state import SyncState
from src.sync_engine import SyncEngine

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / ".sync").mkdir()
    return tmp_path


@pytest.fixture
def config(vault: Path) -> SyncConfig:
    return SyncConfig(
        vault_path=vault,
        drive_folder_id="root_id",
        device_id="smoke_pc",
        credentials_file=vault / "credentials.json",
        token_file=vault / "token.json",
        debounce_seconds=0.05,
        log_file=str(vault / "e2e.log"),
    )


def _make_mock_drive(
    *,
    list_files: list[dict] | None = None,
    get_changes_result: object = ([], "T_new"),
    initial_token: str = "T_init",
) -> MagicMock:
    """mock DriveClient 팩토리.

    - authenticate(): 무동작.
    - upload/delete/rename: 단순 카운트.
    - download: 로컬 경로에 "remote content" 쓰기.
    - list_all_files: 주어진 리스트 반환.
    - get_changes: 인자/예외 지원.
    - get_initial_token: 지정 토큰 반환.
    """
    drive = MagicMock()
    drive.authenticate = MagicMock()
    drive.get_initial_token = MagicMock(return_value=initial_token)

    counter = {"n": 0}

    def _upload(
        local: Path,
        rel: str,
        existing_id: str | None = None,
        *,
        app_properties: dict | None = None,
    ) -> dict:
        counter["n"] += 1
        fid = existing_id or f"uploaded_{counter['n']}"
        return {"id": fid, "md5Checksum": None, "appProperties": app_properties or {}}

    drive.upload = MagicMock(side_effect=_upload)

    def _download(file_id: str, local_path: Path) -> dict:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"remote content")
        return {"id": file_id, "md5Checksum": None, "appProperties": {}}

    drive.download = MagicMock(side_effect=_download)
    drive.hard_delete = MagicMock()
    drive.move_to_tombstones = MagicMock()
    drive.rename = MagicMock()
    # 새 파일의 rel_path 해석 — 테스트에서는 파일명을 그대로 경로로 사용.
    drive.resolve_vault_rel_path = MagicMock(
        side_effect=lambda parents, name: name if name else None
    )

    drive.list_all_files = MagicMock(return_value=list_files or [])

    if isinstance(get_changes_result, Exception):
        drive.get_changes = MagicMock(side_effect=get_changes_result)
    elif callable(get_changes_result):
        drive.get_changes = MagicMock(side_effect=get_changes_result)
    else:
        drive.get_changes = MagicMock(return_value=get_changes_result)

    return drive


def _build_context_with_mock_drive(
    config: SyncConfig,
    drive: MagicMock,
    *,
    with_token_refresh: bool = True,
) -> tuple[AppContext, TokenRefreshCoordinator | None]:
    """mock drive 주입형 AppContext 빌더."""
    shutdown_event = asyncio.Event()
    state = SyncState(config)
    resolver = ConflictResolver(config.device_id, config.vault_path)
    engine = SyncEngine(drive, state, resolver)
    reconciler = Reconciler(state, drive)
    watcher = LocalWatcher(
        config.vault_path, engine, debounce_seconds=config.debounce_seconds
    )
    token_refresh: TokenRefreshCoordinator | None = None
    if with_token_refresh:
        token_refresh = TokenRefreshCoordinator(
            drive, reconciler, engine, state, shutdown_event
        )
    poller = AdaptivePoller(
        drive,
        engine,
        watcher,
        state,
        on_token_invalid=token_refresh,
    )
    ctx = AppContext(
        config=config,
        drive=drive,
        state=state,
        engine=engine,
        reconciler=reconciler,
        watcher=watcher,
        poller=poller,
        shutdown_event=shutdown_event,
    )
    return ctx, token_refresh


async def _run_and_shutdown(ctx: AppContext, after: float = 0.2) -> int:
    """run_app을 태스크로 실행하고 after초 뒤 shutdown_event를 세팅한다."""

    async def _shutdown_trigger() -> None:
        await asyncio.sleep(after)
        ctx.shutdown_event.set()

    shutdown_task = asyncio.create_task(_shutdown_trigger())
    try:
        code = await asyncio.wait_for(run_app(ctx), timeout=10.0)
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
    return code


# ── S1 cold start ───────────────────────────────────────────────────────


class TestScenarioColdStart:
    """state 없음 → run_without_state → Drive 파일 3건 다운로드."""

    async def test_cold_start_downloads_remote_files(
        self, vault: Path, config: SyncConfig
    ) -> None:
        # Drive에 3개 파일이 존재하는 것으로 모킹
        remote_files = [
            {
                "id": "rid_a",
                "name": "a.md",
                "mimeType": "text/markdown",
                "modifiedTime": "2026-04-17T10:00:00.000Z",
                "relative_path": "a.md",
            },
            {
                "id": "rid_b",
                "name": "b.md",
                "mimeType": "text/markdown",
                "modifiedTime": "2026-04-17T10:00:00.000Z",
                "relative_path": "sub/b.md",
            },
            {
                "id": "rid_c",
                "name": "c.md",
                "mimeType": "text/markdown",
                "modifiedTime": "2026-04-17T10:00:00.000Z",
                "relative_path": "sub/nested/c.md",
            },
        ]
        drive = _make_mock_drive(list_files=remote_files)

        ctx, _ = _build_context_with_mock_drive(config, drive)
        exit_code = await _run_and_shutdown(ctx, after=0.2)

        assert exit_code == 0
        # 3개 파일이 로컬에 다운로드되어야 한다
        assert (vault / "a.md").exists()
        assert (vault / "sub/b.md").exists()
        assert (vault / "sub/nested/c.md").exists()
        # 3개 파일이 state에 등록되어 있다
        assert "a.md" in ctx.state.files
        assert "sub/b.md" in ctx.state.files
        assert "sub/nested/c.md" in ctx.state.files
        # state 파일이 flush되어 디스크에 저장되었다
        state_file = vault / ".sync" / "sync_state.json"
        assert state_file.exists()
        # page_token이 초기 토큰으로 세팅되어야 한다
        assert ctx.state.page_token == "T_init"


# ── S2 warm start ───────────────────────────────────────────────────────


class TestScenarioWarmStart:
    """기존 state + 로컬 신규 파일 → reconciler.run이 upload 발행."""

    async def test_warm_start_uploads_new_local_file(
        self, vault: Path, config: SyncConfig
    ) -> None:
        # 기존 state 파일을 만들어 놓는다 (v1, page_token=T0, 빈 files)
        state_file = vault / ".sync" / "sync_state.json"
        state_file.write_text(
            '{"version": 2, "device_id": "smoke_pc", '
            '"page_token": "T0", "last_synced_at": null, "files": {}}',
            encoding="utf-8",
        )
        # 로컬에 신규 파일 1건
        (vault / "new.md").write_text("local content", encoding="utf-8")

        # Drive는 changes 없음 (빈 응답)
        drive = _make_mock_drive(get_changes_result=([], "T1"))

        ctx, _ = _build_context_with_mock_drive(config, drive)
        exit_code = await _run_and_shutdown(ctx, after=0.2)

        assert exit_code == 0
        # 새 로컬 파일이 upload됨
        drive.upload.assert_called()
        uploaded_paths = [call.args[1] for call in drive.upload.call_args_list]
        assert "new.md" in uploaded_paths
        # state.files에 new.md가 drive_id와 함께 기록됨
        assert "new.md" in ctx.state.files
        assert ctx.state.files["new.md"].drive_id is not None


# ── S3 watcher × poller 교차 이벤트 ────────────────────────────────────


class TestScenarioCrossEvents:
    """watcher + poller 동시 이벤트 → engine 잠금으로 직렬 처리, 손실 0."""

    async def test_cross_events_are_serialized(
        self, vault: Path, config: SyncConfig
    ) -> None:
        # 기존 state (warm start, 빈 files)
        (vault / ".sync" / "sync_state.json").write_text(
            '{"version": 2, "device_id": "smoke_pc", '
            '"page_token": "T0", "last_synced_at": null, "files": {}}',
            encoding="utf-8",
        )

        # 첫 get_changes는 빈 응답 → 기동 완료
        drive = _make_mock_drive(get_changes_result=([], "T1"))

        ctx, _ = _build_context_with_mock_drive(config, drive)

        async def _trigger_cross_events() -> None:
            # 초기 reconcile 완료 대기
            await asyncio.sleep(0.1)

            # 로컬 파일 3개를 watcher 이벤트로 주입
            for rel in ("x.md", "y.md", "z.md"):
                p = vault / rel
                p.write_text("content", encoding="utf-8")
                ctx.watcher.on_modified(
                    SimpleNamespace(
                        src_path=str(p), dest_path=None, is_directory=False
                    )
                )

            # 동시에 poller가 remote 변경 1건을 받는 것을 시뮬레이션
            # handle_remote_changes 직접 호출로 잠금 경합 상황 재현
            remote_change = {
                "file_id": "rid_remote",
                "removed": False,
                "file": {
                    "name": "remote_only.md",
                    "modified_time": "2026-04-17T10:00:00.000Z",
                    "md5": "abc",
                },
            }
            ctx.engine.handle_remote_changes([remote_change])

            # 디바운스 만료 대기
            await asyncio.sleep(0.2)
            ctx.shutdown_event.set()

        trigger = asyncio.create_task(_trigger_cross_events())
        try:
            exit_code = await asyncio.wait_for(run_app(ctx), timeout=10.0)
        finally:
            if not trigger.done():
                trigger.cancel()

        assert exit_code == 0
        # 로컬 3개 모두 upload
        uploaded_paths = {call.args[1] for call in drive.upload.call_args_list}
        assert {"x.md", "y.md", "z.md"}.issubset(uploaded_paths)
        # 원격 신규도 download
        drive.download.assert_called()
        # 최종 state에 3개 로컬 + 1개 원격 = 4개
        assert "x.md" in ctx.state.files
        assert "y.md" in ctx.state.files
        assert "z.md" in ctx.state.files
        assert "remote_only.md" in ctx.state.files


# ── S4 TokenInvalidError ────────────────────────────────────────────────


class TestScenarioTokenInvalid:
    """poller가 TokenInvalidError 수신 → TokenRefreshCoordinator 재진입."""

    async def test_token_invalid_triggers_run_without_state(
        self, vault: Path, config: SyncConfig
    ) -> None:
        # 기존 state 존재 (warm start)
        (vault / ".sync" / "sync_state.json").write_text(
            '{"version": 2, "device_id": "smoke_pc", '
            '"page_token": "T_old", "last_synced_at": null, "files": {}}',
            encoding="utf-8",
        )

        # 첫 get_changes는 초기 reconcile에서 정상 응답, 두 번째(poll_once)에서 410
        # → run_without_state 재진입 경로가 TokenRefreshCoordinator를 통해 호출됨
        call_count = {"n": 0}

        def _get_changes(token: str) -> tuple[list, str]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [], "T_updated"
            raise TokenInvalidError("410")

        remote_files = [
            {
                "id": "rid_z",
                "name": "z.md",
                "mimeType": "text/markdown",
                "modifiedTime": "2026-04-17T10:00:00.000Z",
                "relative_path": "z.md",
            }
        ]
        drive = _make_mock_drive(
            list_files=remote_files,
            get_changes_result=_get_changes,
            initial_token="T_refreshed",
        )

        ctx, token_refresh = _build_context_with_mock_drive(config, drive)

        async def _drive_trigger() -> None:
            # 초기 reconcile 통과 후 poller의 poll_once를 직접 한 번 호출하여
            # TokenInvalidError 경로를 확실히 타게 한다.
            await asyncio.sleep(0.1)
            await ctx.poller.poll_once()
            # run_without_state 완료 대기
            await asyncio.sleep(0.1)
            ctx.shutdown_event.set()

        trigger = asyncio.create_task(_drive_trigger())
        try:
            exit_code = await asyncio.wait_for(run_app(ctx), timeout=10.0)
        finally:
            if not trigger.done():
                trigger.cancel()

        assert exit_code == 0
        # token_invalid_signal가 발사됨 (poll_once에서 TokenInvalidError 수신)
        assert ctx.poller.token_invalid_signal is True
        # TokenRefreshCoordinator가 run_without_state를 호출하여 list_all_files 사용
        assert drive.list_all_files.call_count >= 1
        # 재발급된 page_token이 state에 반영됨
        assert ctx.state.page_token == "T_refreshed"
        # 원격 파일이 로컬에 다운로드됨
        assert (vault / "z.md").exists()


# ── smoke 전체 실행 시간 ────────────────────────────────────────────────


class TestSmokeSuitePerformance:
    """smoke 파일 전체가 상수 시간 안에 끝나는지 체감 검증."""

    async def test_single_scenario_under_15_seconds(
        self, vault: Path, config: SyncConfig
    ) -> None:
        drive = _make_mock_drive(list_files=[])
        ctx, _ = _build_context_with_mock_drive(config, drive)

        t0 = time.monotonic()
        exit_code = await _run_and_shutdown(ctx, after=0.1)
        elapsed = time.monotonic() - t0

        assert exit_code == 0
        assert elapsed < 15.0
