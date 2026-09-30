"""IMAP, and nothing about MCP (AD-1).

Two measured facts about this server shape the module.

``imap.yandex.ru:993`` advertises ``AUTH=PLAIN``, and the mailbox signs in with an
app password over ``LOGIN`` -- the way mail programs connect to Yandex. An earlier
version used OAuth (XOAUTH2), which needed a registered application; the operator
asked why, and there was no answer beyond an unmeasured claim that IMAP would not
take an app password. OAuth stays in the core, for Disk.  Its capability line carries **no** ``SORT``,
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
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import anyio.to_thread
from imap_tools import MailBox
from imap_tools.errors import (
    ImapToolsError,
    MailboxFolderSelectError,
    MailboxFolderStatusError,
    MailboxLoginError,
)

from yandex_core.errors import (
    AuthError,
    NotFound,
    PolicyError,
    ProtocolError,
    TransportError,
)

from .body import decode_part, html_to_text, text_part
from .headers import (
    AttachmentPart,
    attachment_presence,
    attachments,
    decode_header_value,
    header_fields,
    parse_addresses,
    parse_fetch_response,
)

__all__ = [
    "NOSELECT_FLAG",
    "FolderRef",
    "HeaderRecord",
    "IMAPMailClient",
    "MessageScan",
    "MessageText",
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

PasswordProvider = Callable[[], Awaitable[str]]

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


#: Header items asked for when reading a message's headers. PEEK, always: a
#: listing that marked mail read would change the operator's mailbox by looking.
_HEADER_ITEMS = (
    "(UID INTERNALDATE RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT)])"
)

#: UIDs per FETCH command, so one command line stays well inside what servers
#: accept. The scan budget in `tools/` bounds the total; this bounds one request.
_FETCH_CHUNK = 200


@dataclass(frozen=True, slots=True)
class HeaderRecord:
    """One message's headers, decoded, and nothing from its body."""

    uid: int
    date: datetime | None
    sender: tuple[str, str] | None
    to: tuple[tuple[str, str], ...]
    subject: str
    flags: tuple[str, ...]
    size: int | None


@dataclass(slots=True)
class MessageScan:
    """What one connection learned about a folder's messages in a date window."""

    #: Every UID the date search returned, ascending as the server gives them.
    uids: list[int]
    uidvalidity: int | None
    #: Headers for the UIDs the caller chose to read.
    read: list[HeaderRecord] = field(default_factory=list)
    #: Attachment presence for the UIDs the caller chose to keep. ``None`` is
    #: "could not be told", never "no".
    attachments: dict[int, bool | None] = field(default_factory=dict)


@dataclass(slots=True)
class MessageText:
    """One message's headers and its text, rendered and decoded -- never its body raw."""

    header: HeaderRecord
    uidvalidity: int | None
    text: str
    #: "plain", "html" (rendered to text), or "none" when the message has no text.
    format: str
    notes: list[str] = field(default_factory=list)


NO_TEXT = (
    "This message has no text part -- only attachments, or nothing at all -- so "
    "there is nothing to read here. It is not an empty letter."
)
CONVERTED = (
    "This message has no plain text; its HTML was converted to readable text. "
    "Layout is approximate; wording and links are kept."
)


#: Given every UID in the window and the folder's UIDVALIDITY, the UIDs to read.
ChooseFn = Callable[[list[int], "int | None"], list[int]]
#: Given the headers read, the UIDs to return -- the only ones whose
#: BODYSTRUCTURE is fetched.
KeepFn = Callable[[list[HeaderRecord]], list[int]]


class IMAPMailClient:
    """One IMAP connection per call, exposed as async methods.

    The password is fetched through a provider rather than held, so it is read
    from the keychain per call and never sits in a long-lived object -- the same
    discipline the calendar client keeps.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        login: str,
        password_provider: PasswordProvider,
        timeout: int = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._login = login
        self._password_provider = password_provider
        self._timeout = timeout

    async def list_folders(self, *, count_for: CountSelector) -> list[FolderRef]:
        """Every folder the server lists, with counts for the chosen ones.

        ``count_for`` is handed the whole listing and returns the names worth a
        ``STATUS``.  It exists so that one connection does both steps without
        this module deciding which folders matter -- that decision is paging,
        and paging lives above.
        """
        password = await self._password_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._list_folders_blocking(password, count_for)
        )

    async def list_messages(
        self,
        *,
        folder: str,
        since: date,
        before: date,
        choose: ChooseFn,
        keep: KeepFn,
    ) -> MessageScan:
        """One read-only connection: date search, bounded header read, structure.

        The server is asked for dates and nothing else (AD-12). Which UIDs are
        read, and which are kept, is decided above through ``choose`` and
        ``keep`` -- paging and filtering are ``tools/``' business -- so this
        module spends exactly the requests those decisions call for.
        """
        password = await self._password_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._list_messages_blocking(
                password, folder, since, before, choose, keep
            )
        )

    async def read_text(self, *, folder: str, uid: int) -> MessageText:
        """One message's text: its text part only, never its attachments.

        Measured: a 28 MB message's text is 14 KB and arrives in 43 ms; the whole
        message takes 2.4 s. The part is found in BODYSTRUCTURE first.
        """
        password = await self._password_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._read_text_blocking(password, folder, uid)
        )

    async def list_attachments(
        self, *, folder: str, uid: int
    ) -> tuple[HeaderRecord, list[AttachmentPart]]:
        """A message's attachments from BODYSTRUCTURE. No content is transferred."""
        password = await self._password_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._list_attachments_blocking(password, folder, uid)
        )

    async def fetch_attachment(
        self, *, folder: str, uid: int, section: str
    ) -> tuple[AttachmentPart, bytes]:
        """One attachment's decoded bytes -- that part alone, PEEKed."""
        password = await self._password_provider()
        return await anyio.to_thread.run_sync(
            lambda: self._fetch_attachment_blocking(password, folder, uid, section)
        )

    # -- blocking half -----------------------------------------------------

    def _list_folders_blocking(
        self, password: str, count_for: CountSelector
    ) -> list[FolderRef]:
        with self._connected(password) as box:
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

    def _list_messages_blocking(
        self,
        password: str,
        folder: str,
        since: date,
        before: date,
        choose: ChooseFn,
        keep: KeepFn,
    ) -> MessageScan:
        with self._connected(password) as box:
            # EXAMINE, not SELECT: nothing done through this connection can set
            # \Seen, whatever a later FETCH asks for.
            self._examine(box, folder)
            client = box.client  # type: ignore[attr-defined]
            uidvalidity = _uidvalidity(client)
            try:
                typ, data = client.uid(
                    "SEARCH", "SINCE", _imap_day(since), "BEFORE", _imap_day(before)
                )
            except ImapToolsError as exc:  # pragma: no cover - imaplib raises its own
                raise ProtocolError(f"The date search in {folder!r} failed.") from exc
            if typ != "OK":
                raise ProtocolError(
                    f"{self._host} refused the date search in {folder!r} ({typ})."
                )
            uids = sorted(int(u) for u in (data[0] or b"").split()) if data else []
            scan = MessageScan(uids=uids, uidvalidity=uidvalidity)

            chosen = choose(uids, uidvalidity)
            scan.read = self._headers(client, chosen)
            kept = keep(scan.read)
            scan.attachments = self._structures(client, kept)
            return scan

    def _examine(self, box: object, folder: str) -> None:
        try:
            box.folder.set(folder, readonly=True)  # type: ignore[attr-defined]
        except MailboxFolderSelectError as exc:
            raise NotFound(
                f"There is no folder named {folder!r} in {self._login}'s "
                "mailbox. Folder names are exact, hierarchy included -- list "
                "them with `mail_folders_list` and pass one back verbatim."
            ) from exc

    def _read_text_blocking(self, password: str, folder: str, uid: int) -> MessageText:
        with self._connected(password) as box:
            self._examine(box, folder)
            client = box.client  # type: ignore[attr-defined]
            uidvalidity = _uidvalidity(client)
            headers = self._headers(client, [uid])
            if not headers:
                raise NotFound(
                    f"There is no message with UID {uid} in {folder!r}. UIDs come "
                    "from `mail_messages_list` for the same folder; one taken from "
                    "another folder names nothing here."
                )
            typ, data = client.uid("FETCH", str(uid), "(UID BODYSTRUCTURE)")
            try:
                records = parse_fetch_response(data) if typ == "OK" else []
            except ValueError:
                records = []
            part = text_part(records[0].bodystructure) if records else None
            if part is None:
                return MessageText(headers[0], uidvalidity, "", "none", [NO_TEXT])

            notes: list[str] = []
            for candidate in (part, *part.fallbacks):
                raw = self._section(client, uid, candidate.section)
                if raw is None:
                    continue
                text, note = decode_part(
                    raw, encoding=candidate.encoding, charset=candidate.charset
                )
                if candidate.subtype == "plain" and not text.strip():
                    continue  # a blank plain part; the HTML, if any, is next
                if note:
                    notes.append(note)
                if candidate.subtype == "html":
                    return MessageText(
                        headers[0],
                        uidvalidity,
                        html_to_text(text),
                        "html",
                        [CONVERTED, *notes],
                    )
                return MessageText(headers[0], uidvalidity, text, "plain", notes)
            return MessageText(headers[0], uidvalidity, "", "none", [NO_TEXT])

    def _message_parts(
        self, client: object, folder: str, uid: int
    ) -> tuple[HeaderRecord, list[AttachmentPart]]:
        headers = self._headers(client, [uid])
        if not headers:
            raise NotFound(
                f"There is no message with UID {uid} in {folder!r}. UIDs come from "
                "`mail_messages_list` for the same folder."
            )
        typ, data = client.uid("FETCH", str(uid), "(UID BODYSTRUCTURE)")  # type: ignore[attr-defined]
        try:
            records = parse_fetch_response(data) if typ == "OK" else []
        except ValueError:
            records = []
        if not records:
            raise ProtocolError(
                f"{self._host} would not describe the structure of message {uid}, so "
                "its attachments cannot be listed. That is not the same as none."
            )
        return headers[0], attachments(records[0].bodystructure)

    def _list_attachments_blocking(
        self, password: str, folder: str, uid: int
    ) -> tuple[HeaderRecord, list[AttachmentPart]]:
        with self._connected(password) as box:
            self._examine(box, folder)
            return self._message_parts(box.client, folder, uid)  # type: ignore[attr-defined]

    def _fetch_attachment_blocking(
        self, password: str, folder: str, uid: int, section: str
    ) -> tuple[AttachmentPart, bytes]:
        with self._connected(password) as box:
            self._examine(box, folder)
            client = box.client  # type: ignore[attr-defined]
            _, parts = self._message_parts(client, folder, uid)
            part = next((p for p in parts if p.section == section), None)
            if part is None:
                raise NotFound(
                    f"Message {uid} has no attachment at part {section!r}. Parts come "
                    "from `mail_attachments_list`; the message's own text is not one."
                )
            raw = self._section(client, uid, section)
            if raw is None:
                raise ProtocolError(
                    f"{self._host} returned nothing for part {section}."
                )
            return part, _transfer_decode(raw, part.encoding)

    def _section(self, client: object, uid: int, section: str) -> bytes | None:
        typ, data = client.uid("FETCH", str(uid), f"(BODY.PEEK[{section}])")  # type: ignore[attr-defined]
        if typ != "OK":
            return None
        chunks = [item[1] for item in data if isinstance(item, tuple)]
        return b"".join(chunks) if chunks else None

    def _headers(self, client: object, uids: list[int]) -> list[HeaderRecord]:
        records: list[HeaderRecord] = []
        for chunk in _chunks(uids, _FETCH_CHUNK):
            typ, data = client.uid("FETCH", _uid_set(chunk), _HEADER_ITEMS)  # type: ignore[attr-defined]
            if typ != "OK":
                raise ProtocolError(f"{self._host} refused to return message headers.")
            try:
                parsed = parse_fetch_response(data)
            except ValueError as exc:
                raise ProtocolError(
                    f"{self._host} returned message headers this server could not "
                    "read, so none are reported rather than some."
                ) from exc
            for raw in parsed:
                fields = header_fields(raw.header)
                senders = parse_addresses(fields.get("from"))
                records.append(
                    HeaderRecord(
                        uid=raw.uid,
                        date=raw.internaldate,
                        sender=senders[0] if senders else None,
                        to=tuple(parse_addresses(fields.get("to"))),
                        subject=decode_header_value(fields.get("subject")),
                        flags=raw.flags,
                        size=raw.size,
                    )
                )
        return records

    def _structures(self, client: object, uids: list[int]) -> dict[int, bool | None]:
        """Attachment presence for exactly these UIDs; unknown where unreadable."""
        presence: dict[int, bool | None] = dict.fromkeys(uids)
        for chunk in _chunks(uids, _FETCH_CHUNK):
            try:
                typ, data = client.uid("FETCH", _uid_set(chunk), "(UID BODYSTRUCTURE)")  # type: ignore[attr-defined]
                parsed = parse_fetch_response(data) if typ == "OK" else []
            except (ImapToolsError, ValueError, OSError):
                # The headers are already in hand. Losing the page over the one
                # item that says whether there is a paperclip would trade a
                # certain answer for an unknown one; it stays unknown instead.
                continue
            for raw in parsed:
                if raw.uid in presence:
                    presence[raw.uid] = attachment_presence(raw.bodystructure)
        return presence

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

    def _connected(self, password: str) -> object:
        """A logged-in mailbox, or this project's taxonomy instead of the library's.

        ``initial_folder=None`` matters: ``imap_tools`` defaults it to ``INBOX``
        and *selects* that folder while signing in. Listing folders needs no
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
            return box.login(self._login, password, initial_folder=None)
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
    """Tell "the sign-in was refused" apart from "your organisation forbids this".

    Measured live, Yandex answers two different problems with one message:
    "[AUTHENTICATIONFAILED] LOGIN invalid credentials or IMAP is disabled". Their
    fixes are in different places, so both are named. The password is never
    quoted: errors get pasted into issues and logs.
    """
    detail = f"{exc}".lower()
    if any(marker in detail for marker in _POLICY_MARKERS):
        return PolicyError(
            f"This organisation's policy does not allow mail programs to sign in "
            f"to {login} on {host}. That is a Yandex 360 administrator setting, "
            "not a problem with the password -- ask them to allow IMAP access "
            "for mail programs."
        )
    return AuthError(
        f"{host} refused the sign-in for {login}. Yandex gives one message for "
        "two different causes, so check both: the app password must be one "
        "created for Mail (Почта) -- a password made for Calendar is refused "
        "here -- and IMAP access must be switched on in Yandex Mail, under "
        "Settings, Mail programs. Then run `yandex-mcp setup mail` to store the "
        "right password."
    )


_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_day(value: date) -> str:
    """IMAP's `date` -- month names in English whatever the process locale."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _uid_set(uids: list[int]) -> str:
    return ",".join(str(u) for u in uids)


def _uidvalidity(client: object) -> int | None:
    """The folder's UIDVALIDITY, as the server announced it on EXAMINE.

    Measured: Yandex sends it, and imaplib keeps it among the untagged responses.
    ``None`` if a server ever does not; a cursor then cannot vouch for itself,
    and the tool refuses to resume from one.
    """
    try:
        _, values = client.response("UIDVALIDITY")  # type: ignore[attr-defined]
        value = values[0] if values else None
        return int(value) if value is not None else None
    except (TypeError, ValueError, IndexError):
        return None


def _transfer_decode(raw: bytes, encoding: str) -> bytes:
    import base64
    import binascii
    import quopri

    try:
        if encoding == "base64":
            return base64.b64decode(b"".join(raw.split()), validate=True)
        if encoding == "quoted-printable":
            return quopri.decodestring(raw)
    except (binascii.Error, ValueError) as exc:
        # A file is exact or it is useless: no best-effort bytes.
        raise ProtocolError(
            f"The attachment claims {encoding} encoding but is not valid {encoding}; "
            "nothing was written."
        ) from exc
    return raw
