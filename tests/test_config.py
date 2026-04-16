"""src/config.py 단위 테스트."""
from __future__ import annotations

import pytest
import yaml

from src.config import IGNORE_PATTERNS, SyncConfig, should_ignore


@pytest.fixture
def valid_yaml_config(tmp_path):
    """유효한 config.yaml을 생성하고 경로를 반환한다."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}", encoding="utf-8")

    config_data = {
        "watch_paths": [{"path": str(vault_dir), "hooks": ["sync"]}],
        "drive": {
            "credentials_file": str(creds_file),
            "token_file": str(tmp_path / "token.json"),
            "folder_id": "test_folder_id_123",
        },
        "sync": {
            "debounce_seconds": 3,
            "poll_interval_seconds": 60,
            "delete_local": True,
        },
        "logging": {
            "level": "DEBUG",
            "file": "test.log",
            "max_bytes": 1024,
            "backup_count": 1,
        },
    }

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(config_data, allow_unicode=True), encoding="utf-8"
    )
    return config_path


class TestSyncConfigFromYaml:
    """SyncConfig.from_yaml 테스트."""

    def test_load_valid_config(self, valid_yaml_config):
        """유효한 config.yaml을 정상적으로 로드한다."""
        config = SyncConfig.from_yaml(valid_yaml_config)

        assert config.vault_path.exists()
        assert config.drive_folder_id == "test_folder_id_123"
        assert config.debounce_seconds == 3.0
        assert config.delete_local is True
        assert config.log_level == "DEBUG"
        assert config.log_file == "test.log"
        assert config.log_max_bytes == 1024
        assert config.log_backup_count == 1

    def test_device_id_defaults_to_hostname(self, valid_yaml_config):
        """device_id가 없으면 hostname을 사용한다."""
        import socket

        config = SyncConfig.from_yaml(valid_yaml_config)
        assert config.device_id == socket.gethostname()

    def test_device_id_from_yaml(self, valid_yaml_config):
        """device_id가 YAML에 있으면 그 값을 사용한다."""
        # 기존 YAML에 device_id 추가
        raw = yaml.safe_load(valid_yaml_config.read_text(encoding="utf-8"))
        raw["device_id"] = "my_custom_pc"
        valid_yaml_config.write_text(
            yaml.dump(raw, allow_unicode=True), encoding="utf-8"
        )

        config = SyncConfig.from_yaml(valid_yaml_config)
        assert config.device_id == "my_custom_pc"

    def test_missing_config_file_exits(self, tmp_path):
        """존재하지 않는 설정 파일이면 sys.exit(1)."""
        with pytest.raises(SystemExit):
            SyncConfig.from_yaml(tmp_path / "nonexistent.yaml")

    def test_empty_folder_id_exits(self, tmp_path):
        """drive.folder_id가 비어있으면 sys.exit(1)."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text("{}", encoding="utf-8")

        config_data = {
            "watch_paths": [{"path": str(vault_dir)}],
            "drive": {
                "credentials_file": str(creds_file),
                "token_file": "token.json",
                "folder_id": "",
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(config_data, allow_unicode=True), encoding="utf-8"
        )

        with pytest.raises(SystemExit):
            SyncConfig.from_yaml(config_path)

    def test_missing_credentials_exits(self, tmp_path):
        """credentials 파일이 없으면 sys.exit(1)."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()

        config_data = {
            "watch_paths": [{"path": str(vault_dir)}],
            "drive": {
                "credentials_file": str(tmp_path / "missing_creds.json"),
                "token_file": "token.json",
                "folder_id": "some_id",
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(config_data, allow_unicode=True), encoding="utf-8"
        )

        with pytest.raises(SystemExit):
            SyncConfig.from_yaml(config_path)

    def test_default_values(self, tmp_path):
        """optional 키 누락 시 기본값이 적용된다."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text("{}", encoding="utf-8")

        config_data = {
            "watch_paths": [{"path": str(vault_dir)}],
            "drive": {
                "credentials_file": str(creds_file),
                "token_file": "token.json",
                "folder_id": "some_id",
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(config_data, allow_unicode=True), encoding="utf-8"
        )

        config = SyncConfig.from_yaml(config_path)
        assert config.debounce_seconds == 2.0
        assert config.delete_local is False
        assert config.log_level == "INFO"
        assert config.log_max_bytes == 5 * 1024 * 1024


class TestSyncConfigProperties:
    """SyncConfig 프로퍼티 테스트."""

    def test_state_dir(self, valid_yaml_config):
        config = SyncConfig.from_yaml(valid_yaml_config)
        assert config.state_dir == config.vault_path / ".sync"

    def test_state_file(self, valid_yaml_config):
        config = SyncConfig.from_yaml(valid_yaml_config)
        assert config.state_file == config.vault_path / ".sync" / "sync_state.json"


class TestShouldIgnore:
    """should_ignore 함수 테스트."""

    def test_obsidian_directory(self):
        assert should_ignore(".obsidian/workspace.json") is True
        assert should_ignore(".obsidian/plugins/data.json") is True

    def test_sync_directory(self):
        assert should_ignore(".sync/sync_state.json") is True

    def test_trash_directory(self):
        assert should_ignore(".trash/deleted.md") is True

    def test_smart_env_directory(self):
        assert should_ignore(".smart-env/cache.json") is True

    def test_ds_store(self):
        assert should_ignore(".DS_Store") is True
        assert should_ignore("subdir/.DS_Store") is True

    def test_tmp_files(self):
        assert should_ignore("draft.tmp") is True
        assert should_ignore("notes/backup.tmp") is True

    def test_normal_files_not_ignored(self):
        assert should_ignore("notes/hello.md") is False
        assert should_ignore("daily/2026-04-14.md") is False
        assert should_ignore("projects/ALL_FOR_ONE.md") is False

    def test_korean_filenames(self):
        assert should_ignore("20. Area/24. 활용도구/메모.md") is False

    def test_nested_obsidian_path(self):
        assert should_ignore("some/path/.obsidian/config") is True

    def test_backslash_normalization(self):
        """Windows 경로(백슬래시)도 올바르게 처리한다."""
        assert should_ignore(".obsidian\\workspace.json") is True
        assert should_ignore("notes\\hello.md") is False

    def test_git_directory(self):
        assert should_ignore(".git/HEAD") is True

    def test_thumbs_db(self):
        assert should_ignore("Thumbs.db") is True

    def test_swp_files(self):
        assert should_ignore("note.md.swp") is True


class TestIgnorePatterns:
    """IGNORE_PATTERNS 상수 테스트."""

    def test_has_at_least_8_patterns(self):
        """sprint contract: IGNORE_PATTERNS가 8개 이상 포함."""
        assert len(IGNORE_PATTERNS) >= 8

    def test_covers_required_patterns(self):
        """spec §7의 필수 제외 패턴이 모두 포함되어 있다."""
        # 디렉토리 umbrella 또는 구체 경로로 커버되어야 함
        required = [".obsidian", ".sync", ".trash", ".DS_Store", "*.tmp"]
        as_text = "|".join(IGNORE_PATTERNS)
        for pattern in required:
            assert pattern in as_text, f"누락: {pattern}"
