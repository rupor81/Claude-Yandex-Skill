"""Unit-test fixtures.

Two things must never happen in this directory: a network call, and a touch of
the operator's real keychain or config. Both are cut off here for every test.
"""

from __future__ import annotations

import pytest
from yandex_core import config as config_module


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point config and the credential fallback file at a temporary directory."""
    monkeypatch.setenv(config_module.CONFIG_DIR_ENV_VAR, str(tmp_path / "config"))
    monkeypatch.delenv(config_module.PROFILE_ENV_VAR, raising=False)
    return tmp_path / "config"


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Make the keychain look empty and unwritable, so tests use the fallback."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)

    def refuse(*args, **kwargs):
        raise RuntimeError("no keyring backend in tests")

    monkeypatch.setattr(keyring, "set_password", refuse)
    monkeypatch.setattr(keyring, "delete_password", refuse)


@pytest.fixture(autouse=True)
def no_credential_env(monkeypatch):
    monkeypatch.delenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", raising=False)
