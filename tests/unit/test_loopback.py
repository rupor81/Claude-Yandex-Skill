"""The loopback listener that receives the browser back after sign-in.

These use a real socket on 127.0.0.1 -- nothing leaves the machine -- because a
listener tested against a fake socket proves only that it agrees with the fake.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from yandex_core.errors import ProtocolError
from yandex_core.loopback import CallbackListener


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _visit_later(url: str, delay: float = 0.05) -> threading.Thread:
    """Play the browser: arrive at the redirect a moment after we start waiting."""

    def go():
        time.sleep(delay)
        # A 404 page is still a visit; what matters is that the browser arrived.
        with contextlib.suppress(Exception):
            urllib.request.urlopen(url, timeout=5).read()

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread


def test_the_browser_arriving_with_a_code_is_received():
    port = _free_port()
    with CallbackListener(port=port) as listener:
        _visit_later(f"http://127.0.0.1:{port}/callback?code=12345&state=abc")
        answer = listener.wait(timeout=5)

    assert answer == {"code": "12345", "state": "abc"}


def test_the_browser_is_shown_that_it_can_go_back_to_the_terminal():
    """The operator is looking at the browser. It should say what just happened."""
    port = _free_port()
    body: dict = {}

    def visit():
        time.sleep(0.05)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback?code=1&state=s", timeout=5
        ) as response:
            body["html"] = response.read().decode("utf-8")
            body["type"] = response.headers.get("Content-Type", "")

    with CallbackListener(port=port) as listener:
        threading.Thread(target=visit, daemon=True).start()
        listener.wait(timeout=5)
        time.sleep(0.1)

    assert "text/html" in body["type"]
    assert "терминал" in body["html"].lower() or "terminal" in body["html"].lower()


def test_a_favicon_request_does_not_end_the_wait():
    """Browsers ask for /favicon.ico on their own. That is not the answer."""
    port = _free_port()
    with CallbackListener(port=port) as listener:
        _visit_later(f"http://127.0.0.1:{port}/favicon.ico", delay=0.02)
        _visit_later(f"http://127.0.0.1:{port}/callback?code=real&state=s", delay=0.2)
        answer = listener.wait(timeout=5)

    assert answer["code"] == "real"


def test_nobody_arriving_times_out_rather_than_hanging_forever():
    port = _free_port()
    with (
        CallbackListener(port=port) as listener,
        pytest.raises(ProtocolError) as caught,
    ):
        listener.wait(timeout=0.2)

    assert "nothing was stored" in str(caught.value).lower()


def test_a_port_already_in_use_is_named_before_the_browser_opens():
    """Binding first means the operator is not sent to sign in for nothing."""
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]

        with pytest.raises(ProtocolError) as caught, CallbackListener(port=port):
            pass

    assert str(port) in str(caught.value)


def test_it_listens_on_loopback_only_never_on_the_network():
    """A listener on 0.0.0.0 would accept a forged callback from the next desk."""
    port = _free_port()
    with CallbackListener(port=port) as listener:
        assert listener.host == "127.0.0.1"


def test_a_login_can_be_run_again_straight_away():
    """What "the listener is released" means to the operator.

    Rebinding with a bare socket fails here for a reason that is not a leak: the
    browser's connection sits in TIME_WAIT for a while after closing. A second
    login binds the way the first did, with address reuse on, so that is the
    thing asserted -- an operator who mistypes their password and retries must
    not be told the port is busy.
    """
    port = _free_port()
    with CallbackListener(port=port) as listener:
        _visit_later(f"http://127.0.0.1:{port}/callback?code=1&state=s")
        listener.wait(timeout=5)

    with CallbackListener(port=port) as again:
        _visit_later(f"http://127.0.0.1:{port}/callback?code=2&state=s")
        assert again.wait(timeout=5)["code"] == "2"


def test_an_error_from_yandex_is_passed_up_as_received():
    """`error=access_denied` is the operator pressing Cancel; it is an answer."""
    port = _free_port()
    with CallbackListener(port=port) as listener:
        _visit_later(f"http://127.0.0.1:{port}/callback?error=access_denied&state=s")
        answer = listener.wait(timeout=5)

    assert answer["error"] == "access_denied"


# The decision about what an answer *means* lives in oauth, not here: this
# module only receives. Asserted so the two do not drift into each other.
def test_the_listener_itself_never_judges_the_answer():
    import yandex_core.loopback as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "checked_state" not in source
    assert "exchange_code" not in source
