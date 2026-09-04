"""The credential module is the only reader of secrets, and it never leaks one."""

from __future__ import annotations

import pytest
from yandex_core.credentials import (
    REDACTED,
    get_secret,
    redact_mapping,
    secret_env_var,
    store_secret,
)
from yandex_core.errors import AuthError

SECRET = "hunter2-app-password"


def test_missing_credential_points_at_setup():
    with pytest.raises(AuthError) as caught:
        get_secret("calendar", "default")
    message = str(caught.value)
    assert "yandex-mcp setup calendar" in message
    assert SECRET not in message


def test_environment_is_consulted_first(monkeypatch):
    monkeypatch.setenv(secret_env_var("calendar", "default"), SECRET)
    assert get_secret("calendar", "default") == SECRET


def test_fallback_file_round_trip_is_owner_only(isolated_config):
    location = store_secret("calendar", "default", SECRET)
    assert "0600" in location

    path = isolated_config / "credentials" / "calendar.default"
    assert path.stat().st_mode & 0o777 == 0o600
    assert get_secret("calendar", "default") == SECRET


def test_world_readable_fallback_file_is_refused(isolated_config):
    store_secret("calendar", "default", SECRET)
    path = isolated_config / "credentials" / "calendar.default"
    path.chmod(0o644)

    with pytest.raises(AuthError) as caught:
        get_secret("calendar", "default")
    assert "chmod 600" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_empty_secret_is_refused():
    with pytest.raises(AuthError):
        store_secret("calendar", "default", "")


def test_redaction_is_by_field_name_and_recursive():
    cleaned = redact_mapping(
        {
            "login": "someone@yandex.ru",
            "password": SECRET,
            "nested": {"app_password": SECRET, "url": "https://caldav.yandex.ru"},
        }
    )
    assert cleaned["login"] == "someone@yandex.ru"
    assert cleaned["password"] == REDACTED
    assert cleaned["nested"]["app_password"] == REDACTED
    assert SECRET not in repr(cleaned)
