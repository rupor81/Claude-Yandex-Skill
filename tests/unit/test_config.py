"""Profiles come from a file and an environment variable, never from a tool call."""

from __future__ import annotations

import pytest

from yandex_core.config import (
    DEFAULT_CALDAV_URL,
    PROFILE_ENV_VAR,
    Profile,
    load_profile,
    selected_profile_name,
    write_profile,
)
from yandex_core.errors import ProtocolError


def test_missing_config_points_at_setup():
    with pytest.raises(ProtocolError) as caught:
        load_profile()
    assert "yandex-mcp setup calendar" in str(caught.value)


def test_written_profile_round_trips():
    write_profile(Profile(name="personal", login="me@yandex.ru"))
    loaded = load_profile()
    assert loaded.name == "personal"
    assert loaded.login == "me@yandex.ru"
    assert loaded.caldav_url == DEFAULT_CALDAV_URL


def test_environment_selects_among_profiles(monkeypatch):
    write_profile(Profile(name="personal", login="me@yandex.ru"))
    write_profile(Profile(name="work", login="me@company.ru"), make_default=False)

    monkeypatch.setenv(PROFILE_ENV_VAR, "work")
    assert selected_profile_name() == "work"
    assert load_profile().login == "me@company.ru"


def test_unknown_profile_names_what_exists(monkeypatch):
    write_profile(Profile(name="personal", login="me@yandex.ru"))
    monkeypatch.setenv(PROFILE_ENV_VAR, "nope")
    with pytest.raises(ProtocolError) as caught:
        load_profile()
    assert "personal" in str(caught.value)


def test_unknown_top_level_and_profile_keys_survive_a_write():
    """The file is read-modify-written; keys this module does not know stay put."""
    import tomllib

    from yandex_core.config import config_path

    write_profile(Profile(name="personal", login="me@yandex.ru"))
    path = config_path()
    path.write_text(
        'later_setting = "keep me"\n'
        + path.read_text(encoding="utf-8")
        + '\n[profiles.personal.extras]\ncolour = "blue"\n',
        encoding="utf-8",
    )

    write_profile(Profile(name="work", login="me@company.ru"), make_default=False)

    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["later_setting"] == "keep me"
    assert document["profiles"]["personal"]["extras"]["colour"] == "blue"
    assert document["profiles"]["work"]["login"] == "me@company.ru"


def test_values_needing_escaping_round_trip():
    """A quote or a backslash in a value must not corrupt the file."""
    awkward = 'me"quote\\slash@yandex.ru'
    write_profile(Profile(name="odd", login=awkward))
    assert load_profile("odd").login == awkward


@pytest.mark.parametrize(
    "name", ["has space", "has.dot", 'has"quote', "", "has/slash", "..", "héllo"]
)
def test_a_profile_name_that_is_not_a_plain_identifier_is_refused(name):
    with pytest.raises(ProtocolError):
        write_profile(Profile(name=name, login="me@yandex.ru"))


def test_not_making_a_default_leaves_a_defaultless_file_defaultless():
    """`make_default=False` must never quietly promote the first profile."""
    import tomllib

    from yandex_core.config import config_path

    write_profile(Profile(name="work", login="me@company.ru"), make_default=False)
    document = tomllib.loads(config_path().read_text(encoding="utf-8"))
    assert "default_profile" not in document

    with pytest.raises(ProtocolError):
        load_profile()  # resolves to "default", which does not exist


def test_an_existing_default_is_not_disturbed():
    write_profile(Profile(name="personal", login="me@yandex.ru"))
    write_profile(Profile(name="work", login="me@company.ru"), make_default=False)
    assert selected_profile_name() == "personal"


def test_a_profile_that_is_not_a_table_is_a_protocol_error():
    from yandex_core.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('default_profile = "x"\n\n[profiles]\nx = "not a table"\n', "utf-8")
    with pytest.raises(ProtocolError):
        load_profile("x")


def test_a_profile_without_a_login_is_a_protocol_error():
    from yandex_core.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'default_profile = "x"\n\n[profiles.x]\ncaldav_url = "https://example.test"\n',
        "utf-8",
    )
    with pytest.raises(ProtocolError) as caught:
        load_profile("x")
    assert "login" in str(caught.value)


def test_a_stray_name_key_inside_a_profile_does_not_duplicate_the_keyword():
    from yandex_core.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'default_profile = "x"\n\n[profiles.x]\nname = "something else"\n'
        'login = "me@yandex.ru"\n',
        "utf-8",
    )
    profile = load_profile("x")
    assert profile.name == "x"
    assert profile.login == "me@yandex.ru"


# -- the mail additions, epic 2 -------------------------------------------


def test_mail_hosts_default_to_the_ones_that_were_measured_to_work():
    """587 is the conventional submission port and it is a dead end here.

    Measured against the live server: smtp.yandex.ru:587 closes the connection
    immediately, with no greeting. A reader who "corrects" 465 to 587 because
    587 is standard produces a connector that cannot send at all, so the
    default is asserted rather than left to whoever edits this next.
    """
    profile = Profile(name="personal", login="me@yandex.ru")

    assert (profile.imap_host, profile.imap_port) == ("imap.yandex.ru", 993)
    assert (profile.smtp_host, profile.smtp_port) == ("smtp.yandex.ru", 465)


def test_a_profile_without_mail_set_up_has_no_client_id():
    """Calendar needs none, and inventing one would make `login mail` look done."""
    assert Profile(name="personal", login="me@yandex.ru").oauth_client_id is None


def test_the_client_id_round_trips_and_is_not_a_secret_store():
    """It travels in the authorization URL by design, so it belongs in config.

    The keychain is for the refresh token. Putting a public identifier there
    would mean the operator cannot see or edit what their connector claims to be.
    """
    write_profile(
        Profile(name="personal", login="me@yandex.ru", oauth_client_id="abc123")
    )
    assert load_profile().oauth_client_id == "abc123"

    from yandex_core.config import config_path

    assert "abc123" in config_path().read_text(), "the client_id is not in the file"


def test_setting_up_calendar_again_does_not_forget_the_mail_login():
    """`setup calendar` writes a Profile that knows nothing about mail.

    If that write dropped the client_id, an operator repairing their calendar
    password would silently break their mailbox, and nothing would say so.
    """
    write_profile(
        Profile(name="personal", login="me@yandex.ru", oauth_client_id="abc123")
    )
    write_profile(Profile(name="personal", login="me@yandex.ru"))

    assert load_profile().oauth_client_id == "abc123"


def test_hosts_written_by_hand_into_the_file_are_honoured():
    """A Yandex 360 domain may front these on its own names."""
    write_profile(Profile(name="personal", login="me@yandex.ru"))
    from yandex_core.config import config_path

    path = config_path()
    path.write_text(
        path.read_text().replace(
            "[profiles.personal]", '[profiles.personal]\nimap_host = "imap.example.ru"'
        ),
        encoding="utf-8",
    )

    assert load_profile().imap_host == "imap.example.ru"


def test_a_non_default_host_given_to_write_profile_is_not_silently_dropped():
    """Found by a mail-setup test: hosts were never written, so one passed in was
    lost with no error. Defaults still stay out of the file."""
    write_profile(
        Profile(name="personal", login="me@yandex.ru", imap_host="imap.example.ru")
    )
    from yandex_core.config import config_path

    assert load_profile().imap_host == "imap.example.ru"
    assert "smtp_host" not in config_path().read_text(), "a default was written"


def test_a_login_from_the_environment_needs_no_config_file(monkeypatch):
    """What a Claude extension passes from its install dialog. No setup command."""
    monkeypatch.setenv("YANDEX_MCP_LOGIN", "me@yandex.ru")
    profile = load_profile()
    assert (profile.name, profile.login) == ("default", "me@yandex.ru")


def test_a_login_from_the_environment_wins_over_the_file(monkeypatch):
    write_profile(
        Profile(name="default", login="old@yandex.ru", caldav_url="https://c.example")
    )
    monkeypatch.setenv("YANDEX_MCP_LOGIN", "new@yandex.ru")
    profile = load_profile()
    assert profile.login == "new@yandex.ru"
    assert profile.caldav_url == "https://c.example", "the rest of the file was lost"


def test_a_blank_login_variable_is_ignored(monkeypatch):
    """An optional extension field left blank arrives as an empty string."""
    write_profile(Profile(name="default", login="me@yandex.ru"))
    monkeypatch.setenv("YANDEX_MCP_LOGIN", "  ")
    assert load_profile().login == "me@yandex.ru"
