"""A message's text: which part, how it is decoded, how HTML becomes text.

Measured on the operator's INBOX, 120 recent messages:

* 72 have no plain text at all, only HTML. Rendering HTML is the ordinary path.
* Whole messages weigh up to 28 MB -- attachments. The text part of that message is
  14 KB and arrives in 43 ms; the whole message takes 2.4 s. So the text part is
  located in BODYSTRUCTURE and fetched on its own.
* One real message carries an empty `text/plain` of 0 bytes ahead of the real one.
  "Take the first plain part" returns an empty letter marked complete.
* Charsets seen: utf-8, koi8-r, windows-1251. Transfer encodings: base64, QP, 8bit,
  7bit.
"""

from __future__ import annotations

import base64
import binascii
import html
import quopri
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = [
    "TextPart",
    "decode_part",
    "html_to_text",
    "strip_quotes",
    "text_part",
]


@dataclass(frozen=True, slots=True)
class TextPart:
    """Where a message's text is, and how it was encoded."""

    section: str
    subtype: str  # "plain" or "html"
    encoding: str
    charset: str
    size: int | None
    #: Further candidates, in preference order, for when this one turns out blank.
    fallbacks: tuple[TextPart, ...] = field(default=())


def text_part(structure: object) -> TextPart | None:
    """The part to read as the message's text, or ``None`` if it has none.

    The first non-attachment ``text/plain`` with content; else the first
    ``text/html``. Zero-size parts are skipped. Attachments -- even text ones --
    are never the body: a `log.txt` attached is not what the sender wrote.
    """
    if not isinstance(structure, list) or not structure:
        return None
    plains: list[TextPart] = []
    htmls: list[TextPart] = []
    try:
        _collect(structure, (), plains, htmls)
    except (IndexError, TypeError, ValueError):
        return None
    candidates = [p for p in plains if p.size != 0] + htmls
    if not candidates:
        return None
    first, rest = candidates[0], tuple(candidates[1:])
    return TextPart(
        first.section, first.subtype, first.encoding, first.charset, first.size, rest
    )


def _collect(
    node: list[object], path: tuple[int, ...], plains: list, htmls: list
) -> None:
    if isinstance(node[0], list):
        for index, item in enumerate(node, start=1):
            if not isinstance(item, list):
                break
            _collect(item, (*path, index), plains, htmls)
        return
    kind, subtype = str(node[0]).lower(), str(node[1]).lower()
    if kind != "text" or subtype not in {"plain", "html"}:
        return
    disposition = node[9] if len(node) > 9 else None
    if (
        isinstance(disposition, list)
        and disposition
        and str(disposition[0]).lower() == "attachment"
    ):
        return
    params = node[2] if isinstance(node[2], list) else []
    keys = [str(k).lower() for k in params[0::2]]
    charset = (
        str(params[2 * keys.index("charset") + 1]) if "charset" in keys else "us-ascii"
    )
    size = node[6] if isinstance(node[6], int) else None
    part = TextPart(
        section=".".join(str(i) for i in path) or "1",
        subtype=subtype,
        encoding=str(node[5] or "7bit").lower(),
        charset=charset.lower(),
        size=size,
    )
    (plains if subtype == "plain" else htmls).append(part)


# -- decoding --------------------------------------------------------------------


def decode_part(raw: bytes, *, encoding: str, charset: str) -> tuple[str, str | None]:
    """Transfer-decode, then charset-decode. Returns the text and a note when
    anything had to be approximated. Never raises: the caller reports the note."""
    note = None
    data = raw
    try:
        if encoding == "base64":
            data = base64.b64decode(b"".join(raw.split()), validate=True)
        elif encoding == "quoted-printable":
            data = quopri.decodestring(raw)
    except (binascii.Error, ValueError):
        note = (
            f"The text part claims {encoding} encoding but is not valid {encoding}; "
            "what could be read is shown as it arrived."
        )
        data = raw
    try:
        text = data.decode(charset, "replace")
    except LookupError:
        note = (
            f"The text part names a character set this server does not know "
            f"({charset}); it was read as UTF-8, and some characters may be wrong."
        )
        text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), note


# -- HTML ------------------------------------------------------------------------------

_BLOCKS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "aside",
    "nav",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "tr",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "pre",
    "form",
    "fieldset",
    "address",
    "center",
    "hr",
}
#: Written as an escape: a literal one is indistinguishable from a space on screen.
NBSP = "\u00a0"

_HIDDEN = {"script", "style", "head", "title", "template", "noscript"}


class _Renderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden = 0
        self.quote = 0
        self.cell = False
        self.links: list[tuple[str, int]] = []

    def _newline(self, count: int = 1) -> None:
        self.out.append("\n" * count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self.hidden += 1
            return
        if self.hidden:
            return
        if tag == "br":
            self._newline()
        elif tag == "li":
            self._newline()
            self.out.append("- ")
        elif tag in {"td", "th"}:
            if self.cell:
                self.out.append(" | ")
            self.cell = True
        elif tag == "tr":
            self._newline()
            self.cell = False
        elif tag == "blockquote":
            self.quote += 1
            self._newline()
        elif tag in _BLOCKS:
            self._newline(2)
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append((href, len(self.out)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._newline()
        elif tag in _BLOCKS or tag == "li":
            self._newline(2 if tag in _BLOCKS else 1)
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            label = "".join(self.out[start:]).strip()
            if href.startswith(("http://", "https://", "mailto:")) and href.rstrip(
                "/"
            ) != label.rstrip("/"):
                self.out.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        text = re.sub(r"\s+", " ", data.replace(NBSP, " "))
        if self.quote and self.out and self.out[-1].endswith("\n"):
            text = text.lstrip()
            if text:
                text = "> " * self.quote + text
        self.out.append(text)


def html_to_text(markup: str) -> str:
    """A readable rendering of HTML mail. Never raises on malformed markup."""
    renderer = _Renderer()
    try:
        renderer.feed(markup)
        renderer.close()
    except Exception:  # noqa: BLE001 -- html.parser is lenient; this is the last guard
        return re.sub(
            r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))
        ).strip()
    text = "".join(renderer.out)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# -- quoted history -------------------------------------------------------------------------

_HISTORY_STARTS = [
    # Yandex Mail, the most common in the operator's mail (10 of 120): a line
    # "25.09.2026, 10:00, "Name" <address>:".
    re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4},\s*\d{1,2}:\d{2},\s*.*:\s*$", re.M),
    # Outlook, Russian and English.
    re.compile(r"^\s*От:\s.*\n\s*(Отправлено|Дата):", re.M),
    re.compile(r"^\s*From:\s.*\n\s*(Sent|Date):", re.M),
    re.compile(
        r"^-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение|"
        r"Forwarded message)\s*-{0,}\s*$",
        re.M | re.I,
    ),
    # A standard signature delimiter.
    re.compile(r"^-- ?$", re.M),
]


def strip_quotes(text: str) -> tuple[str, int]:
    """Remove quoted history and a trailing signature. Returns the text and how
    many characters were removed -- the caller says so, because removing content
    the operator did not ask to lose would be a silent under-return."""
    cut = len(text)
    for pattern in _HISTORY_STARTS:
        match = pattern.search(text)
        if match and match.start() < cut:
            cut = match.start()
    kept = text[:cut]
    # `>`-quoted lines anywhere in what is kept -- including a reply that quotes
    # first and answers below.
    kept = "\n".join(
        line for line in kept.split("\n") if not line.lstrip().startswith(">")
    )
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip("\n")
    kept = kept + "\n" if kept else ""
    removed = len(text) - len(kept)
    if removed <= 0 or kept.strip() == text.strip():
        return text, 0
    return kept, removed
