"""Tests for the core telegram_html pipeline.

The single defense against broken HTML reaching Telegram — previously 0 tests
(audit 2026-07-02). These lock in the behaviour the MCP-server copy regressed
on before it was deleted and made to import this module:

  1. bare `<` / `>` in prose must be escaped (else Telegram 400, no fallback);
  2. a tag straddling a 4096-char chunk boundary must be balanced per chunk.
"""

from telegram_bot.core.utils.telegram_html import (
    markdown_to_html,
    sanitize_html,
    split_html_message,
)


def test_bare_angle_brackets_are_escaped() -> None:
    """`score < 5` must not reach Telegram as a raw tag opener."""
    out = split_html_message("score < 5 and x > 3")
    assert len(out) == 1
    assert "&lt;" in out[0]
    assert "&gt;" in out[0]
    assert "< 5" not in out[0]  # no raw opener left


def test_sanitize_escapes_prose_but_keeps_whitelisted_tags() -> None:
    out = sanitize_html("a < b and <b>real bold</b>")
    assert "&lt;" in out  # prose `<` escaped
    assert "<b>real bold</b>" in out  # whitelisted tag preserved


def test_markdown_bold_and_code_convert() -> None:
    assert "<b>hi</b>" in markdown_to_html("**hi**")
    assert "<code>x</code>" in markdown_to_html("`x`")


def test_unclosed_tag_in_prose_is_balanced() -> None:
    out = split_html_message("here is a <code> mentioned in prose")
    # Balancer closes the stray opener so Telegram accepts the chunk.
    assert out[0].count("<code>") == out[0].count("</code>")


def test_pre_block_split_across_chunks_stays_balanced() -> None:
    """A multi-line code block longer than the limit must yield balanced chunks."""
    body = "\n".join(f"line number {i} with some filler text" for i in range(400))
    text = f"```\n{body}\n```"
    chunks = split_html_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert chunk.count("<code") == chunk.count("</code>")


def test_short_message_is_single_chunk() -> None:
    assert split_html_message("just a short line") == ["just a short line"]


def test_empty_string_passthrough() -> None:
    assert split_html_message("") == [""]
