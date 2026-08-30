from pathlib import Path

import pytest
from pydantic import ValidationError

from mobile_use_mcp.config import ConfigurationError, RuntimeConfig, format_startup_error


def test_defaults_preserve_local_adb_and_screenshot_budgets() -> None:
    config = RuntimeConfig.defaults()

    assert config.adb_host == "127.0.0.1"
    assert config.adb_port == 5037
    assert config.snapshot_max_elements == 200
    assert config.snapshot_max_text_length == 500
    assert config.log_level == "WARNING"


def test_environment_overrides_are_frozen_and_accept_path_alias(tmp_path: Path) -> None:
    config = RuntimeConfig.from_environment(
        {
            "MOBILE_USE_ADB_PATH": "/opt/tools/adb-wrapper",
            "MOBILE_USE_ADB_HOST": "192.0.2.10",
            "MOBILE_USE_ADB_PORT": "5500",
            "MOBILE_USE_SNAPSHOT_MAX_ELEMENTS": "42",
            "MOBILE_USE_ARTIFACT_ROOT": str(tmp_path),
            "MOBILE_USE_LOG_LEVEL": "info",
        }
    )

    assert config.adb_executable == "/opt/tools/adb-wrapper"
    assert config.adb_path == "/opt/tools/adb-wrapper"
    assert (config.adb_host, config.adb_port) == ("192.0.2.10", 5500)
    assert config.snapshot_max_elements == 42
    assert config.log_level == "INFO"
    with pytest.raises(ValidationError):
        config.adb_port = 1  # type: ignore[misc]


def test_invalid_environment_diagnostic_does_not_include_value() -> None:
    secret = "private-endpoint-value"

    with pytest.raises(ConfigurationError) as caught:
        RuntimeConfig.from_environment(
            {
                "MOBILE_USE_ADB_HOST": secret,
                "MOBILE_USE_ADB_PORT": "not-a-port",
            }
        )

    message = format_startup_error(caught.value)
    assert "MOBILE_USE_ADB_PORT" not in message
    assert secret not in message
    assert "adb_port" in message
