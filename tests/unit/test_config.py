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
