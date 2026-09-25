"""Parsing what Yandex's IMAP server actually sends back.

Every BODYSTRUCTURE here is a real shape from the operator's INBOX, captured on
2026-09-25 with names and addresses masked -- not one derived from the parser.
Where the server was *not* seen doing something (literals inside BODYSTRUCTURE,
say), the case is still covered, because RFC 3501 allows it and a Cyrillic
filename is exactly what would trigger it; those tests say so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from yandex_mail_mcp.client.headers import (
    attachment_presence,
    decode_header_value,
    parse_addresses,
    parse_fetch_response,
    tokenize,
)

# -- three real shapes, masked -------------------------------------------------

ALTERNATIVE = (
    b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 0 1 NIL NIL NIL NIL)'
    b'("text" "plain" ("charset" "utf-8") NIL NIL "quoted-printable" 27615 410 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" 78931 1040 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
)
INVITATION = (
    b'(("text" "plain" ("charset" "koi8-r") NIL NIL "quoted-printable" 2348 35 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "koi8-r") NIL NIL "quoted-printable" 4736 107 NIL NIL NIL NIL)'
    b'("text" "calendar" ("charset" "utf-8") NIL NIL "base64" 6840 88 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL "~" NIL)'
)
SIGNATURE_IMAGES = (
    b'((("text" "plain" ("charset" "utf-8") NIL NIL "base64" 14210 183 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "base64" 39970 513 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
    b'("image" "jpeg" ("name" "~") "~" "~" "base64" 1130 NIL ("inline" ("filename" "~")) NIL NIL)'
    b'("image" "png" ("name" "~") "~" "~" "base64" 19078 NIL ("inline" ("filename" "~")) NIL NIL)'
    b' "related" ("boundary" "~") NIL "~" NIL)'
)
WITH_PDF = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "base64" 120 2 NIL NIL NIL NIL)'
    b'("application" "pdf" ("name" "~") NIL NIL "base64" 88120 NIL'
    b' ("attachment" ("filename" "~")) NIL NIL)'
    b' "mixed" ("boundary" "~") NIL NIL NIL)'
)
PLAIN_ONLY = (
    b'("text" "plain" ("charset" "utf-8") NIL NIL "8bit" 512 12 NIL NIL NIL NIL)'
)


def _tree(raw: bytes):
    (tree,) = tokenize([raw])
    return tree


# -- attachment presence ---------------------------------------------------------


def test_a_plain_message_has_no_attachment():
    assert attachment_presence(_tree(PLAIN_ONLY)) is False


def test_text_and_html_alternatives_are_not_attachments():
    assert attachment_presence(_tree(ALTERNATIVE)) is False


def test_a_meeting_invitation_is_part_of_the_message_not_an_attachment():
    """`text/calendar` inside `alternative` is how an invite is carried."""
    assert attachment_presence(_tree(INVITATION)) is False


def test_signature_images_marked_inline_are_not_attachments():
    """89 of 275 real messages carry a filename; only 47 are attachments.

    The difference is mostly logos in signatures, sent inline inside
    `multipart/related`. Calling those attachments would flag most corporate mail.
    """
    assert attachment_presence(_tree(SIGNATURE_IMAGES)) is False


def test_a_part_marked_attachment_is_an_attachment():
    assert attachment_presence(_tree(WITH_PDF)) is True


def test_a_named_non_text_part_with_no_disposition_is_an_attachment():
    """What older mailers send: a filename, no Content-Disposition at all."""
    raw = (
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("application" "octet-stream" ("name" "~") NIL NIL "base64" 900 NIL NIL NIL NIL)'
        b' "mixed" ("boundary" "~") NIL NIL NIL)'
    )
    assert attachment_presence(_tree(raw)) is True


def test_a_forwarded_message_counts_as_an_attachment():
    raw = (
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("message" "rfc822" NIL NIL NIL "7bit" 4000 ("date" "subject" NIL NIL NIL NIL NIL NIL NIL NIL)'
        b' ("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 100 3 NIL NIL NIL NIL) 80'
        b' NIL ("attachment" ("filename" "~")) NIL NIL)'
        b' "mixed" ("boundary" "~") NIL NIL NIL)'
    )
    assert attachment_presence(_tree(raw)) is True


def test_a_structure_that_cannot_be_read_is_unknown_not_false():
    """ "No attachment" is a claim. A shape nobody can read supports no claim."""
    assert attachment_presence("not a list at all") is None
    assert attachment_presence([]) is None


# -- the tokenizer ---------------------------------------------------------------


def test_nil_numbers_quoted_and_nested_lists():
    (tree,) = tokenize([b'("a" NIL 12 ("b" "c\\"d") x)'])
    assert tree == ["a", None, 12, ["b", 'c"d'], "x"]


def test_a_literal_is_stitched_back_in_where_it_was_cut():
    """imaplib splits a literal into its own tuple element. RFC 3501 allows one
    inside BODYSTRUCTURE, and a Cyrillic filename is what would cause it -- not
    seen from Yandex in 275 messages, covered because it is allowed."""
    parts = [
        (b'("application" "pdf" ("name" {14}', "Отчёт.pdf".encode()),
        b') NIL NIL "base64" 1 NIL NIL NIL NIL)',
    ]
    (tree,) = tokenize(parts)
    assert tree[2] == ["name", "Отчёт.pdf"]


def test_an_unbalanced_response_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        tokenize([b'("a" ("b"'])


# -- a FETCH response ------------------------------------------------------------


def test_a_header_fetch_is_split_into_its_fields_whatever_the_order():
    """The server chooses the order of items; the parser must not."""
    data = [
        (
            b"17 (UID 5606 RFC822.SIZE 4411 FLAGS (\\Seen $Forwarded) "
            b'INTERNALDATE "25-Sep-2026 09:15:02 +0300" '
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT)] {40}",
            b"Subject: hello\r\nFrom: a@b.ru\r\n\r\n\r\n",
        ),
        b")",
    ]
    (record,) = parse_fetch_response(data)

    assert record.uid == 5606
    assert record.size == 4411
    assert record.flags == ("\\Seen", "$Forwarded")
    assert record.internaldate == datetime(
        2026, 9, 25, 9, 15, 2, tzinfo=timezone(timedelta(hours=3))
    )
    assert b"Subject: hello" in record.header


def test_several_messages_in_one_response_are_all_returned():
    data = [
        (
            b'1 (UID 10 INTERNALDATE "01-Sep-2026 00:00:00 +0000" RFC822.SIZE 1 FLAGS () '
            b"BODY[HEADER.FIELDS (SUBJECT)] {4}",
            b"\r\n\r\n",
        ),
        b")",
        (
            b'2 (UID 11 INTERNALDATE "02-Sep-2026 00:00:00 +0000" RFC822.SIZE 2 FLAGS () '
            b"BODY[HEADER.FIELDS (SUBJECT)] {4}",
            b"\r\n\r\n",
        ),
        b")",
    ]
    assert [r.uid for r in parse_fetch_response(data)] == [10, 11]


def test_a_bodystructure_fetch_carries_the_tree():
    data = [b"3 (UID 12 BODYSTRUCTURE " + WITH_PDF + b")"]
    (record,) = parse_fetch_response(data)
    assert attachment_presence(record.bodystructure) is True


# -- decoding ---------------------------------------------------------------------


def test_a_base64_utf8_cyrillic_subject_is_decoded():
    """761 of 817 real subjects in 90 days are encoded-words; decoding is the norm."""
    raw = "=?utf-8?B?0KDQtdC30YPQu9GM0YLQsNGC0Ysg0LLRgdGC0YDQtdGH0Lg=?="
    assert decode_header_value(raw) == "Результаты встречи"


def test_a_koi8_quoted_printable_subject_is_decoded():
    """koi8-r is in the real mailbox, seen in a BODYSTRUCTURE charset above."""
    raw = "=?koi8-r?Q?=F7=D3=D4=D2=C5=DE=C1?="
    assert decode_header_value(raw) == "Встреча"


def test_adjacent_encoded_words_join_without_a_stray_space():
    raw = "=?utf-8?B?0KAt0KTQsNGA0Lw=?= =?utf-8?B?INCQ0LrQutC+0YDQtA==?="
    assert decode_header_value(raw) == "Р-Фарм Аккорд"


def test_a_broken_charset_degrades_to_replacement_text_not_an_exception():
    """One bad header must not cost the caller the whole page."""
    raw = "=?no-such-charset?B?0J/RgNC40LLQtdGC?="
    decoded = decode_header_value(raw)
    assert isinstance(decoded, str) and decoded


def test_addresses_keep_the_decoded_name_and_the_address():
    raw = "=?utf-8?B?0JjQstCw0L0g0J/QtdGC0YDQvtCy?= <ivan@example.ru>, b@example.ru"
    assert parse_addresses(raw) == [
        ("Иван Петров", "ivan@example.ru"),
        ("", "b@example.ru"),
    ]


def test_a_missing_header_is_empty_not_none():
    assert decode_header_value(None) == ""
    assert parse_addresses(None) == []


def test_the_timezone_of_internaldate_survives():
    """AD-7: every instant is timezone-aware; `+0300` is not UTC."""
    data = [
        (
            b'1 (UID 1 INTERNALDATE "25-Sep-2026 09:00:00 +0300" RFC822.SIZE 1 FLAGS () '
            b"BODY[HEADER.FIELDS (SUBJECT)] {4}",
            b"\r\n\r\n",
        ),
        b")",
    ]
    (record,) = parse_fetch_response(data)
    assert record.internaldate.astimezone(UTC) == datetime(
        2026, 9, 25, 6, 0, tzinfo=UTC
    )


def test_a_subject_in_raw_utf8_bytes_is_read_as_text_not_replacement_marks():
    """No encoded-words, just UTF-8 bytes in the header -- common from older
    mailers. compat32's `items()` turned these into `�` before decoding began."""
    from yandex_mail_mcp.client.headers import header_fields

    header = (
        "Subject: Р-Фарм Аккорд: итоги\r\nFrom: Иван <ivan@example.ru>\r\n\r\n".encode()
    )
    fields = header_fields(header)

    assert decode_header_value(fields["subject"]) == "Р-Фарм Аккорд: итоги"
    assert parse_addresses(fields["from"]) == [("Иван", "ivan@example.ru")]
