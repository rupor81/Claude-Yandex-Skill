"""The two paths the rest of the suite deliberately avoids.

`conftest` forces every keyring call to fail, so the primary storage path -- the
one an operator actually gets -- is never exercised there. It is exercised here
against an in-memory fake. Logging is asserted in the same file because both are
about where a secret is allowed to end up.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest
from yandex_core.app import LOG_LEVEL_ENV_VAR, configure_logging
from yandex_core.credentials import (
    KEYRING_SERVICE,
    REDACTED,
    delete_secret,
    get_secret,
    store_secret,
)

SECRET = "hunter2-app-password"


class FakeKeyring:
    """A keychain that works, recording the exact key each call used."""

    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}
        self.keys_used: list[tuple[str, str]] = []

    def set_password(self, service, account, password):
        self.keys_used.append((service, account))
        self.entries[(service, account)] = password

    def get_password(self, service, account):
        self.keys_used.append((service, account))
        return self.entries.get((service, account))

    def delete_password(self, service, account):
        import keyring.errors

        if (service, account) not in self.entries:
            raise keyring.errors.PasswordDeleteError(account)
        del self.entries[(service, account)]


@pytest.fixture
def working_keychain(monkeypatch):
    """Override the autouse fixture that makes every keyring call fail."""
    import keyring

    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


# -- the keychain path -----------------------------------------------------


def test_a_working_keychain_is_used_and_reported(working_keychain):
    location = store_secret("calendar", "default", SECRET)
    assert "keychain" in location.lower()
    assert get_secret("calendar", "default") == SECRET


def test_store_and_retrieve_agree_on_the_key(working_keychain):
    store_secret("calendar", "default", SECRET)
    stored_under = working_keychain.keys_used[0]
    working_keychain.keys_used.clear()

    assert get_secret("calendar", "default") == SECRET
    assert working_keychain.keys_used == [stored_under]
    assert stored_under == (KEYRING_SERVICE, "calendar:default")


def test_profiles_do_not_collide_in_the_keychain(working_keychain):
    store_secret("calendar", "default", SECRET)
    store_secret("calendar", "work", "another-app-password")
    assert get_secret("calendar", "default") == SECRET
    assert get_secret("calendar", "work") == "another-app-password"


def test_no_fallback_file_is_written_when_the_keychain_works(
    working_keychain, isolated_config
):
    store_secret("calendar", "default", SECRET)
    assert not (isolated_config / "credentials" / "calendar.default").exists()


def test_a_keychain_store_clears_a_stale_fallback_file(monkeypatch, isolated_config):
    """A locked keychain later must not fall through to yesterday's password."""
    import keyring

    store_secret("calendar", "default", "outdated-password")
    fallback = isolated_config / "credentials" / "calendar.default"
    assert fallback.exists()

    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    assert store_secret("calendar", "default", SECRET) == "system keychain"

    assert not fallback.exists()
    assert get_secret("calendar", "default") == SECRET


def test_deleting_something_never_stored_is_not_an_error(working_keychain):
    delete_secret("calendar", "default")


def test_a_broken_keychain_backend_does_not_report_a_successful_delete(monkeypatch):
    """Saying a credential is gone when it is not is worse than an error."""
    import keyring

    def explode(*args, **kwargs):
        raise RuntimeError("the keychain is locked")

    monkeypatch.setattr(keyring, "delete_password", explode)
    with pytest.raises(RuntimeError):
        delete_secret("calendar", "default")


def test_the_environment_variable_is_stripped_like_the_file(monkeypatch):
    from yandex_core.credentials import secret_env_var

    monkeypatch.setenv(secret_env_var("calendar", "default"), f"  {SECRET}\n")
    assert get_secret("calendar", "default") == SECRET


# -- logging ---------------------------------------------------------------


@pytest.fixture
def captured_stdout(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    return stream


def stderr_of(record_call) -> str:
    """Run one logging call against the configured handler, returning stderr text."""
    handler = logging.getLogger().handlers[0]
    buffer = io.StringIO()
    original, handler.stream = handler.stream, buffer
    try:
        record_call()
    finally:
        handler.stream = original
    return buffer.getvalue()


def test_the_handler_writes_to_stderr_and_never_to_stdout(captured_stdout):
    configure_logging()
    (handler,) = logging.getLogger().handlers
    assert handler.stream is sys.stderr

    text = stderr_of(lambda: logging.getLogger("probe").info("hello"))
    assert "hello" in text
    assert captured_stdout.getvalue() == ""


def test_a_secret_in_mapping_args_is_redacted():
    configure_logging()
    text = stderr_of(
        lambda: logging.getLogger("probe").info(
            "connecting as %(login)s with %(password)s",
            {"login": "me@yandex.ru", "password": SECRET},
        )
    )
    assert SECRET not in text
    assert REDACTED in text
    assert "me@yandex.ru" in text


def test_a_secret_in_tuple_args_is_redacted():
    """`logger.info("password %s", secret)` is the shape that used to leak."""
    configure_logging()
    text = stderr_of(lambda: logging.getLogger("probe").info("password %s", SECRET))
    assert SECRET not in text
    assert REDACTED in text


def test_a_secret_in_list_args_is_redacted():
    configure_logging()
    text = stderr_of(lambda: logging.getLogger("probe").info("token %s", [SECRET]))
    assert SECRET not in text


def test_a_mapping_nested_in_tuple_args_is_redacted():
    configure_logging()
    text = stderr_of(
        lambda: logging.getLogger("probe").info("profile %s", {"password": SECRET})
    )
    assert SECRET not in text


def test_ordinary_arguments_survive():
    configure_logging()
    text = stderr_of(lambda: logging.getLogger("probe").info("listing %s", "calendars"))
    assert "listing calendars" in text


# -- log level -------------------------------------------------------------


def test_the_environment_sets_the_level(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "debug")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "DEBUG")
    configure_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_an_unrecognised_level_falls_back_to_info_rather_than_raising(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "VERBOSE-ISH")
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_the_default_is_info(monkeypatch):
    monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO
