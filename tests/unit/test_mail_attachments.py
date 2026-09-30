"""Listing and downloading attachments -- what a message carries, and one file of it."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from conftest import FakeMailBox, FakeMessage, install_fake_mailbox, with_messages

from yandex_core.errors import NotFound, ProtocolError
from yandex_mail_mcp.client.headers import attachments, tokenize
from yandex_mail_mcp.client.imap_client import IMAPMailClient
from yandex_mail_mcp.tools.attachments import (
    DOWNLOAD_TOOL,
    LIST_TOOL,
    build_mail_attachment_download,
    build_mail_attachments_list,
)

MSK = timezone(timedelta(hours=3))
PDF_NAME = (
    "=?UTF-8?B?0KPRgdC70L7QstC40Y8g0L/QuNC70L7RgtCwLnBkZg==?="  # Условия пилота.pdf
)
PDF = b"%PDF-1.7 pilot terms " * 500


def _wire(data: bytes) -> bytes:
    """base64 as the server sends it: 76 characters a line, CRLF -- measured."""
    return base64.encodebytes(data).replace(b"\n", b"\r\n")


LETTER = (
    b'((("text" "plain" ("charset" "utf-8") NIL NIL "base64" 120 2 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "base64" 400 5 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
    b'("image" "png" ("name" "logo.png") "~" NIL "base64" 1130 NIL ("inline" ("filename" "logo.png")) NIL NIL)'
    b'("application" "pdf" ("name" "'
    + PDF_NAME.encode()
    + b'") NIL NIL "base64" '
    + str(len(_wire(PDF))).encode()
    + b' NIL ("attachment" ("filename" "'
    + PDF_NAME.encode()
    + b'")) NIL NIL)'
    b'("application" "octet-stream" NIL NIL NIL "base64" 88 NIL ("attachment" NIL) NIL NIL)'
    b' "mixed" ("boundary" "~") NIL NIL NIL)'
)


def _tree(raw):
    (tree,) = tokenize([raw])
    return tree


def _box(**parts):
    message = FakeMessage(
        uid=7,
        when=datetime(2026, 8, 13, 12, 0, tzinfo=MSK),
        structure=LETTER,
        parts={"3": _wire(PDF), "4": _wire(b"\x00\x01"), **parts},
    )
    return with_messages(FakeMailBox(), messages=[message])


def _client():
    async def password():
        return "app-password"

    return IMAPMailClient(
        host="imap.yandex.ru",
        port=993,
        login="me@yandex.ru",
        password_provider=password,
    )


def _list(box, monkeypatch, **call):
    install_fake_mailbox(monkeypatch, box)
    client = _client()

    async def provider():
        return client

    args = {"uid": 7, **call}
    return anyio.run(lambda: build_mail_attachments_list(provider)(**args))


def _download(box, monkeypatch, **call):
    install_fake_mailbox(monkeypatch, box)
    client = _client()

    async def provider():
        return client

    args = {"uid": 7, "part": "3", **call}
    return anyio.run(lambda: build_mail_attachment_download(provider)(**args))


# -- which parts are attachments ---------------------------------------------------


def test_the_walker_finds_exactly_what_has_attachments_counts():
    found = attachments(_tree(LETTER))
    assert [a.section for a in found] == ["3", "4"]


def test_a_named_part_is_decoded_and_typed():
    pdf, _ = attachments(_tree(LETTER))
    assert pdf.filename == "Условия пилота.pdf"
    assert pdf.mime_type == "application/pdf"


def test_an_unnamed_attachment_gets_a_name_from_its_part():
    _, unnamed = attachments(_tree(LETTER))
    assert unnamed.filename == "attachment-4"


# -- listing ------------------------------------------------------------------------


def test_listing_names_the_attachments_and_transfers_no_content(monkeypatch):
    box = _box()

    result = _list(box, monkeypatch)

    assert [a.filename for a in result.attachments] == [
        "Условия пилота.pdf",
        "attachment-4",
    ]
    assert result.attachments[0].part == "3"
    assert result.attachments[0].size_bytes == pytest.approx(len(PDF), rel=0.01)
    assert all("BODY.PEEK[3]" not in items for _, items in box.client.fetched)
    assert box.selections == [("INBOX", True)]


def test_an_unknown_uid_is_not_found(monkeypatch):
    with pytest.raises(NotFound):
        _list(_box(), monkeypatch, uid=8)


# -- downloading ----------------------------------------------------------------------


def test_a_download_writes_the_exact_bytes_and_says_where(monkeypatch, tmp_path):
    result = _download(_box(), monkeypatch, directory=str(tmp_path))

    written = tmp_path / "Условия пилота.pdf"
    assert result.path == str(written)
    assert written.read_bytes() == PDF
    assert result.bytes == len(PDF)


def test_downloading_marks_nothing_read(monkeypatch, tmp_path):
    box = _box()
    _download(box, monkeypatch, directory=str(tmp_path))
    assert box.selections == [("INBOX", True)]
    for _, items in box.client.fetched:
        assert "BODY[" not in items.replace("BODY.PEEK[", "")


def test_an_existing_file_is_not_replaced_without_overwrite(monkeypatch, tmp_path):
    target = tmp_path / "Условия пилота.pdf"
    target.write_bytes(b"mine")

    with pytest.raises(ProtocolError) as caught:
        _download(_box(), monkeypatch, directory=str(tmp_path))

    assert target.read_bytes() == b"mine"
    assert "overwrite" in str(caught.value)


def test_overwrite_replaces_it(monkeypatch, tmp_path):
    (tmp_path / "Условия пилота.pdf").write_bytes(b"mine")
    _download(_box(), monkeypatch, directory=str(tmp_path), overwrite=True)
    assert (tmp_path / "Условия пилота.pdf").read_bytes() == PDF


@pytest.mark.parametrize(
    "name", ["../escape.pdf", "sub/x.pdf", "..", ".hidden", "a\x00b"]
)
def test_a_filename_that_could_leave_the_directory_is_refused_before_any_request(
    name, monkeypatch, tmp_path
):
    box = _box()
    with pytest.raises(ProtocolError):
        _download(box, monkeypatch, directory=str(tmp_path), filename=name)
    assert box.client.fetched == [], "the server was asked anyway"
    assert list(tmp_path.iterdir()) == []


def test_a_malicious_name_from_the_message_is_reduced_not_followed(
    monkeypatch, tmp_path
):
    evil = LETTER.replace(PDF_NAME.encode(), b"../../evil.pdf")
    box = with_messages(
        FakeMailBox(),
        messages=[
            FakeMessage(
                uid=7,
                when=datetime(2026, 8, 13, tzinfo=MSK),
                structure=evil,
                parts={"3": base64.b64encode(PDF)},
            )
        ],
    )
    result = _download(box, monkeypatch, directory=str(tmp_path / "in"))
    assert result.path == str(tmp_path / "in" / "evil.pdf")
    assert not (tmp_path / "evil.pdf").exists()


def test_a_part_that_is_the_body_is_refused(monkeypatch, tmp_path):
    with pytest.raises(NotFound) as caught:
        _download(_box(), monkeypatch, directory=str(tmp_path), part="1.1")
    assert "mail_attachments_list" in str(caught.value)


def test_an_interrupted_write_leaves_no_file_under_the_real_name(monkeypatch, tmp_path):
    import pathlib

    def fail(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "replace", fail)
    with pytest.raises(OSError):
        _download(_box(), monkeypatch, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_the_tools_are_registered_with_honest_risk():
    from yandex_core.risk import RISK_REGISTRY, RiskClass

    assert RISK_REGISTRY[LIST_TOOL] is RiskClass.READ
    # it can replace a local file when asked to: that is a destructive update
    assert RISK_REGISTRY[DOWNLOAD_TOOL] is RiskClass.DESTRUCTIVE


def test_a_symlink_under_the_name_is_not_followed_even_with_overwrite(
    monkeypatch, tmp_path
):
    """The names are plain, but the folder may hold a link named like the file.
    Replacing "through" it would write wherever it points."""
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not yours to touch")
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "Условия пилота.pdf").symlink_to(outside)

    with pytest.raises(ProtocolError):
        _download(_box(), monkeypatch, directory=str(folder), overwrite=True)
    assert outside.read_bytes() == b"not yours to touch"


def test_an_attachment_that_is_not_valid_base64_writes_nothing(monkeypatch, tmp_path):
    """A file is exact or useless: no best-effort bytes under a real name."""
    with pytest.raises(ProtocolError):
        # Right length, stray characters: lenient decoding silently drops them.
        _download(_box(**{"3": b"JVBE!!!!Ri0x"}), monkeypatch, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []
