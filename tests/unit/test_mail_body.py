"""Choosing, decoding and rendering a message's text.

Structures are the real shapes from the operator's INBOX (masked), including the
one that motivated a rule: an empty `text/plain` ahead of the real one.
"""

from __future__ import annotations

import base64
import quopri

from yandex_mail_mcp.client.body import (
    decode_part,
    html_to_text,
    strip_quotes,
    text_part,
)
from yandex_mail_mcp.client.headers import tokenize


def _tree(raw: bytes):
    (tree,) = tokenize([raw])
    return tree


EMPTY_PLAIN_FIRST = (
    b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 0 1 NIL NIL NIL NIL)'
    b'("text" "plain" ("charset" "utf-8") NIL NIL "quoted-printable" 27615 410 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" 78931 1040 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
)
HTML_ONLY_WITH_IMAGES = (
    b'((("text" "html" ("charset" "utf-8") NIL NIL "base64" 23700 300 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
    b'("image" "png" ("name" "~") "~" "~" "base64" 19078 NIL ("inline" ("filename" "~")) NIL NIL)'
    b' "related" ("boundary" "~") NIL NIL NIL)'
)
PLAIN_WITH_PDF = (
    b'(("text" "plain" ("charset" "koi8-r") NIL NIL "base64" 120 2 NIL NIL NIL NIL)'
    b'("application" "pdf" ("name" "~") NIL NIL "base64" 88120 NIL ("attachment" ("filename" "~")) NIL NIL)'
    b' "mixed" ("boundary" "~") NIL NIL NIL)'
)
ATTACHED_TEXT_ONLY = (
    b'(("text" "plain" ("charset" "utf-8" "name" "log.txt") NIL NIL "7bit" 900 20 NIL'
    b' ("attachment" ("filename" "log.txt")) NIL NIL)'
    b'("application" "pdf" ("name" "~") NIL NIL "base64" 88120 NIL ("attachment" ("filename" "~")) NIL NIL)'
    b' "mixed" ("boundary" "~") NIL NIL NIL)'
)
SINGLE_PART = (
    b'("text" "plain" ("charset" "windows-1251") NIL NIL "8bit" 512 12 NIL NIL NIL NIL)'
)


# -- which part -----------------------------------------------------------------------


def test_an_empty_plain_part_ahead_of_the_real_one_is_skipped():
    """Seen in real mail. The first `text/plain` is 0 bytes; taking it returns an
    empty letter marked complete -- a silent loss of the whole message."""
    part = text_part(_tree(EMPTY_PLAIN_FIRST))
    assert part.section == "2"
    assert part.subtype == "plain"
    assert part.encoding == "quoted-printable"


def test_html_is_taken_when_there_is_no_plain_part():
    part = text_part(_tree(HTML_ONLY_WITH_IMAGES))
    assert (part.section, part.subtype, part.encoding) == ("1.1", "html", "base64")


def test_the_body_is_never_an_attachment_even_a_text_one():
    """A `log.txt` attached is not what the sender wrote."""
    assert text_part(_tree(ATTACHED_TEXT_ONLY)) is None


def test_a_single_part_message_is_section_one_with_its_charset():
    part = text_part(_tree(SINGLE_PART))
    assert (part.section, part.charset) == ("1", "windows-1251")


def test_the_plain_alternatives_are_all_offered_so_a_blank_one_can_fall_back():
    part = text_part(_tree(EMPTY_PLAIN_FIRST))
    assert [alt.subtype for alt in part.fallbacks] == ["html"]


# -- decoding ------------------------------------------------------------------------------


def test_base64_koi8r_is_decoded():
    raw = base64.b64encode("Итоги встречи по Аккорду".encode("koi8-r"))
    text, note = decode_part(raw, encoding="base64", charset="koi8-r")
    assert text == "Итоги встречи по Аккорду"
    assert note is None


def test_quoted_printable_windows1251_is_decoded():
    raw = quopri.encodestring("Протокол совещания".encode("cp1251"))
    text, _ = decode_part(raw, encoding="quoted-printable", charset="windows-1251")
    assert text == "Протокол совещания"


def test_an_unknown_charset_degrades_with_a_note_not_an_exception():
    text, note = decode_part(
        "привет".encode(), encoding="8bit", charset="x-no-such-charset"
    )
    assert text == "привет"
    assert note and "x-no-such-charset" in note


def test_broken_base64_is_reported_not_returned_as_empty():
    _, note = decode_part(b"@@@not base64@@@", encoding="base64", charset="utf-8")
    assert note, "an undecodable part came back with no word about it"


def test_crlf_becomes_newline():
    text, _ = decode_part(b"a\r\nb\r\n", encoding="7bit", charset="us-ascii")
    assert text == "a\nb\n"


# -- HTML --------------------------------------------------------------------------------------


def test_script_style_and_head_never_reach_the_text():
    html = (
        "<html><head><title>T</title><style>p{color:red}</style></head>"
        "<body><script>var x=1;</script><p>Итоги</p></body></html>"
    )
    assert html_to_text(html).strip() == "Итоги"


def test_paragraphs_and_breaks_become_lines():
    html = "<p>Первое</p><p>Второе<br>третье</p><div>четвёртое</div>"
    lines = [line for line in html_to_text(html).splitlines() if line.strip()]
    assert lines == ["Первое", "Второе", "третье", "четвёртое"]


def test_list_items_get_a_dash():
    html = "<ul><li>срок</li><li>бюджет</li></ul>"
    assert "- срок" in html_to_text(html)
    assert "- бюджет" in html_to_text(html)


def test_table_cells_are_separated_and_rows_are_lines():
    """Meeting notes arrive as tables often enough that a flattened row --
    `ИвановСрок12.10` -- would be useless."""
    html = "<table><tr><td>Иванов</td><td>срок</td><td>12.10</td></tr><tr><td>Петров</td></tr></table>"
    lines = [line.strip() for line in html_to_text(html).splitlines() if line.strip()]
    assert lines[0] == "Иванов | срок | 12.10"
    assert lines[1] == "Петров"


def test_a_link_keeps_its_target():
    html = '<p>См. <a href="https://docs.example.ru/x">протокол</a></p>'
    assert "протокол (https://docs.example.ru/x)" in html_to_text(html)


def test_a_link_whose_text_is_its_target_is_not_doubled():
    html = '<a href="https://a.ru/">https://a.ru/</a>'
    assert html_to_text(html).strip() == "https://a.ru/"


def test_entities_are_unescaped_and_a_non_breaking_space_is_a_space():
    assert (
        html_to_text("<p>A &amp; B &laquo;C&raquo;&nbsp;D</p>").strip() == "A & B «C» D"
    )


def test_whitespace_in_source_is_collapsed_but_paragraphs_survive():
    html = "<p>  много\n\n   пробелов  </p>\n\n\n<p>дальше</p>"
    text = html_to_text(html)
    assert "много пробелов" in text
    assert "\n\n\n" not in text


def test_blockquote_is_marked_as_quoted():
    """So quoted history in HTML mail can be recognised -- and stripped on request."""
    html = "<p>Ответ</p><blockquote><p>Исходное</p></blockquote>"
    assert "> Исходное" in html_to_text(html)


def test_malformed_html_still_yields_its_text():
    assert "текст" in html_to_text("<div><p>текст<span>без закрытия")


# -- quotes -------------------------------------------------------------------------------------


def test_nothing_is_removed_when_there_is_nothing_quoted():
    text, removed = strip_quotes("Просто письмо.\nБез истории.")
    assert (text, removed) == ("Просто письмо.\nБез истории.", 0)


def test_yandex_style_reply_header_starts_the_quoted_history():
    """The most common marker in the operator's mail: 10 of 120, measured."""
    body = 'Согласен, двигаемся.\n\n25.09.2026, 10:00, "Иван Петров" <ivan@example.ru>:\n> Предлагаю перенести.\n'
    text, removed = strip_quotes(body)
    assert text.strip() == "Согласен, двигаемся."
    assert removed == len(body) - len(text)


def test_outlook_russian_and_english_headers_start_the_history():
    ru = "Ок.\n\nОт: Иван Петров\nОтправлено: 25 сентября 2026 г. 10:00\nКому: me\nТема: x\n\nтекст"
    en = "OK.\n\nFrom: Ivan\nSent: Friday, September 25, 2026 10:00\nTo: me\n\ntext"
    assert strip_quotes(ru)[0].strip() == "Ок."
    assert strip_quotes(en)[0].strip() == "OK."


def test_a_signature_after_the_standard_delimiter_is_removed():
    text, removed = strip_quotes("Текст.\n-- \nИван Петров\nтел. 123\n")
    assert text.strip() == "Текст."
    assert removed > 0


def test_a_leading_quote_block_is_not_mistaken_for_the_whole_reply():
    """A reply that quotes first and answers below must keep the answer."""
    body = "> вопрос\n> ещё\n\nОтвет ниже цитаты.\n"
    text, _ = strip_quotes(body)
    assert "Ответ ниже цитаты." in text
    assert "вопрос" not in text
