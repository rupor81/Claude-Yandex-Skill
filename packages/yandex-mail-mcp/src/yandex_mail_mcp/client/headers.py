"""What Yandex's IMAP server sends back, turned into values this server can trust.

Three jobs, each with a measured reason:

* **A tokenizer for IMAP responses.** ``imaplib`` returns FETCH data half-parsed:
  literals are split into tuples and everything else is raw bytes, and an item
  name such as ``BODY[HEADER.FIELDS (FROM TO SUBJECT)]`` contains spaces and
  parentheses of its own. Matching that with regular expressions is how a subject
  that happens to contain ``(`` corrupts a page.
* **Header decoding.** 761 of 817 real subjects in 90 days were MIME
  encoded-words, some in koi8-r. Decoding is the ordinary case, and one broken
  header degrades to replacement characters rather than costing the whole page.
* **Attachment presence from BODYSTRUCTURE.** Of 275 real messages, 89 carried a
  filename and only 47 an attachment disposition -- the rest were mostly logos in
  signatures, sent inline. The rule here is the one mail clients use, so the
  answer matches what the operator sees in Yandex Mail.
"""

from __future__ import annotations

import email.utils
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import compat32

__all__ = [
    "FetchRecord",
    "Literal",
    "attachment_presence",
    "decode_header_value",
    "parse_addresses",
    "parse_fetch_response",
    "parse_internaldate",
    "tokenize",
]


class Literal(str):
    """An IMAP literal: text for comparison, with its exact bytes kept in `.raw`.

    A header block arrives as a literal and must stay bytes -- it may be koi8-r
    or raw 8-bit -- while a filename literal inside BODYSTRUCTURE is compared as
    text. One type serves both without guessing which one a caller wanted.
    """

    raw: bytes

    def __new__(cls, raw: bytes) -> Literal:
        text = super().__new__(cls, raw.decode("utf-8", "replace"))
        text.raw = raw
        return text


_LITERAL_MARK = object()


def _stream(parts: list[object]) -> list[object]:
    """Flatten imaplib's FETCH data into bytes and literal markers, in order."""
    out: list[object] = []
    for part in parts:
        if isinstance(part, tuple):
            prefix, literal = part[0], part[1]
            # The prefix ends with the `{n}` that announced the literal.
            cut = prefix.rstrip().rfind(b"{")
            out.append(prefix[:cut] if cut >= 0 else prefix)
            out.append((_LITERAL_MARK, literal))
        elif isinstance(part, bytes):
            out.append(part)
    return out


def tokenize(parts: list[object]) -> list[object]:
    """Parse IMAP response data into nested Python values.

    Lists become lists, ``NIL`` becomes ``None``, digits become ``int``, quoted
    strings and atoms become ``str``, literals become :class:`Literal`.

    Raises:
        ValueError: the parentheses do not balance. A response that cannot be
            read whole is not read partly.
    """
    root: list[object] = []
    stack: list[list[object]] = [root]
    for chunk in _stream(parts):
        if isinstance(chunk, tuple):
            stack[-1].append(Literal(chunk[1]))
            continue
        data = chunk
        i, n = 0, len(data)
        while i < n:
            c = data[i : i + 1]
            if c in b" \r\n\t":
                i += 1
            elif c == b"(":
                new: list[object] = []
                stack[-1].append(new)
                stack.append(new)
                i += 1
            elif c == b")":
                if len(stack) == 1:
                    raise ValueError("unbalanced ')' in IMAP response")
                stack.pop()
                i += 1
            elif c == b'"':
                i += 1
                buf = bytearray()
                while i < n and data[i : i + 1] != b'"':
                    if data[i : i + 1] == b"\\" and i + 1 < n:
                        i += 1
                    buf += data[i : i + 1]
                    i += 1
                i += 1  # closing quote
                stack[-1].append(bytes(buf).decode("utf-8", "replace"))
            else:
                start = i
                depth = 0
                while i < n:
                    ch = data[i : i + 1]
                    if ch == b"[":
                        depth += 1
                    elif ch == b"]":
                        depth -= 1
                    elif depth == 0 and ch in b" ()\r\n\t":
                        break
                    i += 1
                atom = data[start:i].decode("utf-8", "replace")
                if atom.upper() == "NIL":
                    stack[-1].append(None)
                elif atom.isdigit():
                    stack[-1].append(int(atom))
                else:
                    stack[-1].append(atom)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in IMAP response")
    return root


# -- one FETCH response ----------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        start=1,
    )
}


def parse_internaldate(value: str) -> datetime:
    """``"25-Sep-2026 09:15:02 +0300"`` as an aware datetime.

    Month names are mapped by hand rather than with ``%b``: ``strptime`` reads
    months in the process locale, and on a Russian-locale machine ``Sep`` is not
    a month.
    """
    day_month_year, clock, offset = value.strip().split()
    day, month, year = day_month_year.split("-")
    hour, minute, second = (int(x) for x in clock.split(":"))
    sign = -1 if offset.startswith("-") else 1
    zone = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
    return datetime(
        int(year), _MONTHS[month.lower()], int(day), hour, minute, second, tzinfo=zone
    )


@dataclass(frozen=True, slots=True)
class FetchRecord:
    """What one message's FETCH item said, before any interpretation."""

    uid: int
    internaldate: datetime | None = None
    flags: tuple[str, ...] = ()
    size: int | None = None
    header: bytes | None = None
    bodystructure: object = None


def parse_fetch_response(data: list[object]) -> list[FetchRecord]:
    """Every message in a FETCH response, whatever order the server chose.

    Raises:
        ValueError: the response is not a sequence of ``n (key value ...)``.
    """
    values = tokenize(data)
    records: list[FetchRecord] = []
    for value in values:
        if not isinstance(value, list):
            continue  # the message sequence number before each list
        fields: dict[str, object] = {}
        for key, item in zip(value[0::2], value[1::2], strict=False):
            fields[str(key).upper()] = item
        uid = fields.get("UID")
        if not isinstance(uid, int):
            raise ValueError("a FETCH item carried no UID")
        header = None
        for key, item in fields.items():
            if key.startswith("BODY[") and isinstance(item, str):
                header = item.raw if isinstance(item, Literal) else item.encode("utf-8")
        date_value = fields.get("INTERNALDATE")
        flags = fields.get("FLAGS")
        size = fields.get("RFC822.SIZE")
        records.append(
            FetchRecord(
                uid=uid,
                internaldate=parse_internaldate(date_value)
                if isinstance(date_value, str)
                else None,
                flags=tuple(str(f) for f in flags) if isinstance(flags, list) else (),
                size=size if isinstance(size, int) else None,
                header=header,
                bodystructure=fields.get("BODYSTRUCTURE"),
            )
        )
    return records


# -- headers -----------------------------------------------------------------------


def decode_header_value(raw: str | None) -> str:
    """A header value as readable text. Never raises: one bad header is not a
    reason to lose a page of good ones."""
    if raw is None:
        return ""
    raw = _undo_surrogates(raw)
    try:
        return str(make_header(decode_header(raw)))
    except (LookupError, UnicodeError, ValueError, TypeError):
        pieces = []
        for part, charset in decode_header(raw):
            if isinstance(part, bytes):
                try:
                    pieces.append(part.decode(charset or "utf-8", "replace"))
                except LookupError:
                    pieces.append(part.decode("utf-8", "replace"))
            else:
                pieces.append(part)
        return "".join(pieces)


def _undo_surrogates(value: str) -> str:
    """Raw 8-bit header bytes arrive from `compat32` as surrogate escapes.

    Read as UTF-8, which is what the measured mailbox's raw headers use; anything
    else degrades to replacement characters rather than to an exception.
    """
    if not any("\udc80" <= ch <= "\udcff" for ch in value):
        return value
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def parse_addresses(raw: str | None) -> list[tuple[str, str]]:
    """``[(name, address), ...]`` with names decoded.

    Split *before* decoding: an encoded-word never contains a comma, but the
    name it decodes to may, and splitting afterwards would cut one person in two.
    """
    if not raw:
        return []
    raw = _undo_surrogates(raw)
    return [
        (decode_header_value(name).strip(), address.strip())
        for name, address in email.utils.getaddresses([raw])
        if name or address
    ]


def header_fields(header: bytes | None) -> dict[str, str]:
    """The raw (still encoded) values of a header block, by lower-case name."""
    if not header:
        return {}
    message = BytesHeaderParser(policy=compat32).parsebytes(header)
    # `raw_items`, not `items`: compat32's `items()` "sanitises" a value holding
    # raw 8-bit bytes into U+FFFD replacement characters before anything here
    # sees it, so a subject written in plain UTF-8 -- no encoded-words -- came
    # back as a row of `�`. The raw value keeps the bytes as surrogate escapes,
    # which `_undo_surrogates` turns back into text. Found by a fake, not by the
    # real mailbox, whose 30-day sample happened to hold no such header.
    return {key.lower(): value for key, value in message.raw_items()}


# -- attachments ----------------------------------------------------------------


def attachment_presence(structure: object) -> bool | None:
    """Whether the message carries an attachment, as mail clients count one.

    ``None`` when the structure cannot be read: "no attachment" is a claim, and a
    shape nobody could read supports no claim.

    A part counts when its disposition is ``attachment``; when it is a forwarded
    message; or when it has a filename, no disposition, and is not text. A part
    marked ``inline`` never counts -- that is how signature logos are sent.
    """
    try:
        if not isinstance(structure, list) or not structure:
            return None
        return _any_attachment(structure)
    except (IndexError, TypeError, AttributeError, ValueError):
        return None


def _any_attachment(part: list[object]) -> bool:
    """RFC 3501 `body-type-mpart`: the sub-parts are the lists that come *first*,
    up to the subtype string. Everything after -- parameters, disposition,
    language -- is also lists, and must not be mistaken for parts."""
    if isinstance(part[0], list):
        subparts = []
        for item in part:
            if not isinstance(item, list):
                break
            subparts.append(item)
        return any(_any_attachment(sub) for sub in subparts)
    return _leaf_is_attachment(part)


def _leaf_is_attachment(leaf: list[object]) -> bool:
    kind = str(leaf[0]).lower()
    subtype = str(leaf[1]).lower()
    if kind == "text":
        disposition_at = 9
    elif kind == "message" and subtype == "rfc822":
        disposition_at = 11
    else:
        disposition_at = 8
    disposition = leaf[disposition_at] if len(leaf) > disposition_at else None
    if isinstance(disposition, list) and disposition:
        mode = str(disposition[0]).lower()
        if mode == "attachment":
            return True
        if mode == "inline":
            return False
    if kind == "message" and subtype == "rfc822":
        return True
    if kind == "text":
        return False
    return _has_name(leaf[2]) or (
        isinstance(disposition, list)
        and len(disposition) > 1
        and _has_name(disposition[1])
    )


def _has_name(params: object) -> bool:
    if not isinstance(params, list):
        return False
    keys = [str(k).lower() for k in params[0::2]]
    return "name" in keys or "filename" in keys
