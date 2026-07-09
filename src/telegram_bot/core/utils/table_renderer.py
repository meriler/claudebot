"""Render markdown tables as PNG images for Telegram."""

from __future__ import annotations

import logging
import platform
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_TABLE_RE = re.compile(r"(?:^\s*\|[^\n]+\|\s*$\n?)+", re.MULTILINE)
_FENCED_RE = re.compile(r"(?:```|~~~)[^\n]*\n.*?(?:```|~~~)", re.DOTALL)

_FONT_SIZE = 15
_CELL_PADDING = 8
_OUTER_PADDING = 10
_LINE_HEIGHT = _FONT_SIZE + 8
_MAX_IMAGE_WIDTH = 700
_MAX_COL_WIDTH = 200
_MIN_COL_WIDTH = 40

_MACOS_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
]
_LINUX_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_EMOJI_MAP: dict[str, str] = {
    "✅": "[OK]",
    "❌": "[X]",
    "✓": "[v]",
    "✗": "[x]",
    "⚠️": "[!]",
    "\U0001f534": "(x)",
    "\U0001f7e2": "(v)",
    "\U0001f7e1": "(!)",
    "\U0001f4cc": "*",
    "\U0001f3af": ">",
    "\U0001f4a1": "*",
    "⭐": "*",
    "\U0001f525": "(!)",
}

_SEPARATOR_RE = re.compile(r"^[\s\-:]+$")
_MARKUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<br\s*/?>", re.IGNORECASE), " "),
    (re.compile(r"<[^>]+>"), ""),
    (re.compile(r"\*\*(.*?)\*\*"), r"\1"),
    (re.compile(r"\*(.*?)\*"), r"\1"),
    (re.compile(r"`(.*?)`"), r"\1"),
]


def _load_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _MACOS_FONTS if platform.system() == "Darwin" else _LINUX_FONTS
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, _FONT_SIZE)
            except Exception:
                continue
    logger.warning("No suitable font found, using PIL default")
    return ImageFont.load_default()


def _text_width(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> int:
    bbox = font.getbbox(text)
    return int(bbox[2] - bbox[0])


def _wrap_text(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str, max_width: int
) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    if _text_width(font, text) <= max_width:
        return [text]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if _text_width(font, candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _has_separator_row(table_text: str) -> bool:
    for line in table_text.strip().splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if cells and all(_SEPARATOR_RE.match(c) for c in cells if c):
                return True
    return False


def _parse_table(table_text: str) -> list[list[str]]:
    if not _has_separator_row(table_text):
        return []
    rows: list[list[str]] = []
    for line in table_text.strip().splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if all(_SEPARATOR_RE.match(c) for c in cells if c):
            continue
        cleaned: list[str] = []
        for cell in cells:
            for pat, repl in _MARKUP_PATTERNS:
                cell = pat.sub(repl, cell)
            for emoji, text_repl in _EMOJI_MAP.items():
                cell = cell.replace(emoji, text_repl)
            cleaned.append(cell.strip())
        rows.append(cleaned)
    return rows


def _compute_col_widths(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    rows: list[list[str]],
    num_cols: int,
) -> list[int]:
    """Compute column widths with word-wrap to fit within _MAX_IMAGE_WIDTH."""
    natural = [0] * num_cols
    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                natural[i] = max(natural[i], _text_width(font, cell))
    natural = [min(w + _CELL_PADDING * 2, _MAX_COL_WIDTH) for w in natural]

    content_budget = _MAX_IMAGE_WIDTH - _OUTER_PADDING * 2
    total = sum(natural)

    if total <= content_budget:
        return natural

    result = [0] * num_cols
    for i in range(num_cols):
        scaled = int(natural[i] * content_budget / total)
        result[i] = max(scaled, _MIN_COL_WIDTH)
    return result


def _wrap_rows(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    rows: list[list[str]],
    col_widths: list[int],
    num_cols: int,
) -> list[list[list[str]]]:
    """Wrap each cell, return rows of columns of lines."""
    wrapped: list[list[list[str]]] = []
    for row in rows:
        wrapped_row: list[list[str]] = []
        for col_idx in range(num_cols):
            cell = row[col_idx] if col_idx < len(row) else ""
            inner_w = col_widths[col_idx] - _CELL_PADDING * 2
            lines = _wrap_text(font, cell, max(inner_w, 20))
            wrapped_row.append(lines)
        wrapped.append(wrapped_row)
    return wrapped


def render_table_as_image(table_text: str) -> str | None:
    """Render a markdown table to a temporary PNG file. Returns path or None."""
    try:
        rows = _parse_table(table_text)
        if len(rows) < 2:
            return None

        font = _load_font()
        num_cols = max(len(r) for r in rows)
        col_widths = _compute_col_widths(font, rows, num_cols)
        wrapped = _wrap_rows(font, rows, col_widths, num_cols)

        row_heights = [max(len(cell_lines) for cell_lines in wr) * _LINE_HEIGHT for wr in wrapped]

        table_w = sum(col_widths) + _OUTER_PADDING * 2
        table_h = sum(row_heights) + _LINE_HEIGHT + _OUTER_PADDING * 2

        img = Image.new("RGB", (table_w, table_h), "#FFFFFF")
        draw = ImageDraw.Draw(img)

        y = _OUTER_PADDING
        for row_idx, (wrapped_row, rh) in enumerate(zip(wrapped, row_heights, strict=True)):
            x = _OUTER_PADDING
            for col_idx in range(num_cols):
                cell_lines = wrapped_row[col_idx] if col_idx < len(wrapped_row) else [""]
                for line_idx, line in enumerate(cell_lines):
                    draw.text(
                        (x + _CELL_PADDING, y + 4 + line_idx * _LINE_HEIGHT),
                        line,
                        fill="#000000",
                        font=font,
                    )
                if col_idx < num_cols - 1:
                    x_right = x + col_widths[col_idx]
                    draw.line([(x_right, y), (x_right, y + rh)], fill="#CCCCCC")
                x += col_widths[col_idx]

            if row_idx == 0:
                y += rh
                draw.line(
                    [(_OUTER_PADDING, y), (table_w - _OUTER_PADDING, y)],
                    fill="#000000",
                    width=2,
                )
            else:
                y += rh

        tl = (_OUTER_PADDING, _OUTER_PADDING)
        br = (table_w - _OUTER_PADDING, table_h - _OUTER_PADDING)
        draw.rectangle([tl, br], outline="#000000", width=2)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png", mode="wb") as tmp:
            img.save(tmp, format="PNG")
        logger.info("Rendered table image: %s (%dx%d)", tmp.name, table_w, table_h)
        return tmp.name
    except Exception:
        logger.exception("Failed to render table as image")
        return None


def _header_summary(table_text: str) -> str:
    """Extract first row of a table as a short text summary for captions."""
    for line in table_text.strip().splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not all(_SEPARATOR_RE.match(c) for c in cells if c):
                return " | ".join(cells)
    return "Table"


def find_tables(text: str) -> list[re.Match[str]]:
    """Return markdown table matches in *text*, skipping fenced code blocks."""
    fenced_spans: set[int] = set()
    for m in _FENCED_RE.finditer(text):
        fenced_spans.update(range(m.start(), m.end()))
    return [m for m in _TABLE_RE.finditer(text) if m.start() not in fenced_spans]
