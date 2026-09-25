"""The loopback listener that receives the browser back after sign-in.

This is the half of the standard desktop OAuth flow that brings the operator
home: the command opens the browser, Yandex lets them sign in however it offers
-- password, QR code, Yandex ID -- and then sends the browser to a local address
where this listener is waiting with the answer.

It only *receives*. Whether the answer is a code, a refusal, or a forgery is
decided in :mod:`yandex_core.oauth`, where the state that proves it is ours
lives. A listener that judged answers too would be two places holding one rule.

It binds **127.0.0.1**, never all interfaces. Anything reachable from the network
could be sent a forged callback by the next machine on the Wi-Fi.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse

from .errors import ProtocolError

__all__ = ["CallbackListener"]

#: What the operator sees in the browser once they are back. It is the only thing
#: on their screen at that moment, so it says what happened and what to do next.
_DONE_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Yandex MCP</title>
<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:15vh auto;
max-width:32em;padding:0 1em;color:#222}</style></head>
<body><h1>Готово</h1>
<p>Яндекс вернул ответ. Можно закрыть эту вкладку и вернуться в терминал &mdash;
там сказано, чем всё закончилось.</p>
<p lang="en">Done. You can close this tab and return to the terminal.</p>
</body></html>
"""


class CallbackListener:
    """One loopback HTTP listener, alive for the duration of one sign-in.

    Used as a context manager so the socket is bound *before* the browser is
    opened -- a busy port is reported while the operator has still done nothing
    -- and released afterwards whatever happened in between.
    """

    host = "127.0.0.1"

    def __init__(self, *, port: int, path: str = "/callback") -> None:
        self.port = port
        self.path = path
        self._answer: dict[str, str] | None = None
        self._arrived = threading.Event()
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> CallbackListener:
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != listener.path:
                    # A browser asks for /favicon.ico on its own. That is not
                    # the answer, and treating it as one would end the wait
                    # with nothing in hand.
                    self.send_response(404)
                    self.end_headers()
                    return
                if not listener._arrived.is_set():
                    listener._answer = dict(urllib.parse.parse_qsl(parts.query))
                    listener._arrived.set()
                body = _DONE_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                # The query string carries the authorization code. The default
                # handler prints every request line to stderr, which would put
                # a live credential in the operator's terminal scrollback.
                return

        try:
            self._server = http.server.HTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            raise ProtocolError(
                f"Port {self.port} on this machine is already in use, so the "
                "browser would have nowhere to come back to. Another login may "
                "still be running -- finish or close it and try again. Nothing "
                "was opened and nothing was stored."
            ) from exc
        self._server.timeout = 0.2
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self._thread.start()
        return self

    def wait(self, *, timeout: float) -> dict[str, str]:
        """The query parameters the browser arrived with.

        Raises:
            ProtocolError: nobody arrived within ``timeout`` seconds.
        """
        if not self._arrived.wait(timeout):
            minutes = timeout / 60
            span = f"{minutes:g} minutes" if minutes >= 1 else f"{timeout:g} seconds"
            raise ProtocolError(
                f"The browser did not come back within {span}. Nothing was "
                "stored. Run the login again when you are ready to sign in."
            )
        return dict(self._answer or {})

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
