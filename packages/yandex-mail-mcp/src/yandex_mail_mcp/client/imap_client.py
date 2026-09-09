"""IMAP, and nothing about MCP (AD-1).

Two measured facts about this server shape the module.

``imap.yandex.ru:993`` advertises ``AUTH=XOAUTH2``, so the OAuth token is used
directly and no SASL is hand-rolled.  Its capability line carries **no** ``SORT``,
``THREAD``, ``ESEARCH`` or ``UTF8=ACCEPT``: there is no server-side ordering to
lean on, and ordering therefore belongs to ``tools/`` where AD-9 already puts it.

``folder.status()`` costs **one request per folder**.  Epic 1 measured that this
account's request budget is per-account, scarce, and does not reset between runs,
so counts are never taken for folders nobody asked about.  Which folders those
are is a question about paging, which is ``tools/``' business -- so the caller
passes a selector and this module asks for exactly what comes back from it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

import anyio.to_thread
from imap_tools import MailBox
from imap_tools.errors import (
    ImapToolsError,
    MailboxFolderStatusError,
    MailboxLoginError,
)

from yandex_core.errors import (
    AuthError,
    PolicyError,
    ProtocolError,
    TransportError,
)

__all__ = [
    "NOSELECT_FLAG",
    "FolderRef",
    "IMAPMailClient",
]

#: A folder marked with this is a node in the hierarchy, not a mailbox: it
#: cannot be selected, so it has no message count to give.
NOSELECT_FLAG = "\\noselect"

#: What is asked of each folder that has counts to give. Kept short on purpose:
#: every additional item is data nobody asked for, on a metered connection.
_STATUS_OPTIONS = ("MESSAGES", "UNSEEN")

#: Substrings a Yandex 360 policy refusal is recognised by. The server does not
#: give a machine-readable code for it, so the wording is all there is.
_POLICY_MARKERS = ("organization", "organisation", "policy", "disabled by")

TokenProvider = Callable[[], Awaitable[str]]

#: Given every folder the server listed, which of them to spend a STATUS on.
CountSelector = Callable[[Sequence["FolderRef"]], Sequence[str]]


@dataclass(frozen=True, slots=True)
class FolderRef:
    """One folder, and what could and could not be learned about it."""

    name: str
    delimiter: str
    flags: tuple[str, ...]
    selectable: bool
    messages: int | None = None
    unseen: int | None = None
    #: Why the counts are absent, when they are. ``None`` means they are present.
    #: Absence with no reason would be indistinguishable from a count of zero
    #: that this server simply failed to report.
    counts_note: str | None = None


class IMAPMailClient:
    """One IMAP connection per call, exposed as async methods.

    The token is fetched through a provider rather than held: it expires, and a
    client that cached one would start failing halfway through a session with an
    error about credentials that are perfectly good.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        login: str,
        access_token_provider: TokenProvider,
        timeout: int = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._login = login
        self._token_provider = access_token_provider
        self._timeout = timeout

    async def list_folders(self, *, count_for: CountSelector) -> list[FolderRef]:
        """Every folder the server lists, with counts for the chosen ones.

        ``count_for`` is handed the whole listing and returns the names worth a
        ``STATUS``.  It exists so that one connection does both steps without
        this module deciding which folders matter -- that decision is paging,
        and paging lives above.
        """
        token = await self._token_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._list_folders_blocking(token, count_for)
        )

    # -- blocking half -----------------------------------------------------

    def _list_folders_blocking(
        self, token: str, count_for: CountSelector
    ) -> list[FolderRef]:
        with self._connected(token) as box:
            try:
                listed = box.folder.list()
            except ImapToolsError as exc:
                raise ProtocolError(
                    f"Could not list the folders of {self._login} on "
                    f"{self._host}: the server refused the request "
                    f"({type(exc).__name__})."
                ) from exc

            folders = [_folder_from(item) for item in listed]
            by_name = {folder.name: index for index, folder in enumerate(folders)}

            for name in count_for(folders):
                index = by_name.get(name)
                if index is None:
                    # The selector named something the listing did not contain.
                    # Skipped rather than raised: it costs nothing, and a page
                    # is not worth losing over a name nobody will look for.
                    continue
                folders[index] = self._with_counts(box, folders[index])
            return folders

    def _with_counts(self, box: object, folder: FolderRef) -> FolderRef:
        """One STATUS, or an honest reason there is none.

        A folder whose STATUS fails is still returned. Dropping it would make
        one server hiccup look like a folder the operator does not have, which
        is the silent under-return NFR3 exists to forbid.
        """
        if not folder.selectable:
            return folder
        try:
            answer = box.folder.status(folder.name, _STATUS_OPTIONS)  # type: ignore[attr-defined]
        except (ImapToolsError, MailboxFolderStatusError) as exc:
            return replace(
                folder,
                counts_note=(
                    "The server would not report counts for this folder "
                    f"({type(exc).__name__}). The folder is there; its counts "
                    "are not known."
                ),
            )
        return replace(
            folder,
            # A key the server omitted is unknown, not zero. Defaulting it would
            # answer a question the server declined to answer.
            messages=_count(answer, "MESSAGES"),
            unseen=_count(answer, "UNSEEN"),
        )

    def _connected(self, token: str) -> object:
        """A logged-in mailbox, or this project's taxonomy instead of the library's.

        ``initial_folder=None`` matters: ``imap_tools`` defaults it to ``INBOX``
        and *selects* that folder while logging in. Listing folders needs no
        selection, and the SELECT is a round trip nobody asked for on a metered
        connection -- and it fails outright on an account with no ``INBOX``.
        """
        try:
            box = MailBox(self._host, self._port, timeout=self._timeout)
        except (OSError, TimeoutError) as exc:
            raise TransportError(
                f"Could not reach {self._host}:{self._port}: the network or the "
                f"host is unavailable ({type(exc).__name__})."
            ) from exc
        try:
            return box.xoauth2(self._login, token, initial_folder=None)
        except MailboxLoginError as exc:
            raise _login_refused(exc, login=self._login, host=self._host) from exc
        except (OSError, TimeoutError) as exc:
            raise TransportError(
                f"Could not reach {self._host}:{self._port}: the network or the "
                f"host is unavailable ({type(exc).__name__})."
            ) from exc


def _count(answer: object, key: str) -> int | None:
    if not isinstance(answer, dict):
        return None
    value = answer.get(key)
    return value if isinstance(value, int) else None


def _folder_from(item: object) -> FolderRef:
    """One `FolderInfo`, with its name already decoded from modified UTF-7.

    The decoding is `imap_tools`' -- verified by roundtrip on Cyrillic names,
    including one containing the hierarchy delimiter -- so nothing is decoded
    here. Doing it twice would corrupt a name that legitimately contains `&`.
    """
    flags = tuple(str(flag) for flag in (getattr(item, "flags", ()) or ()))
    return FolderRef(
        name=str(getattr(item, "name", "") or ""),
        delimiter=str(getattr(item, "delim", "") or ""),
        flags=flags,
        selectable=not any(flag.lower() == NOSELECT_FLAG for flag in flags),
    )


def _login_refused(exc: Exception, *, login: str, host: str) -> Exception:
    """Tell "your token is wrong" apart from "your organisation forbids this".

    They send the operator to different places, and only one of them is
    something they can fix themselves. Nothing in the message quotes the token:
    errors get pasted into issues and logs.
    """
    detail = f"{exc}".lower()
    if any(marker in detail for marker in _POLICY_MARKERS):
        return PolicyError(
            f"This organisation's policy does not allow this application to "
            f"sign in to {login} on {host}. That is a Yandex 360 administrator "
            "setting, not a problem with the authorisation -- ask them to "
            "permit external clients, or to allow this application."
        )
    return AuthError(
        f"{host} refused the mail authorisation for {login}. Run "
        "`yandex-mcp login mail` to authorise again."
    )
