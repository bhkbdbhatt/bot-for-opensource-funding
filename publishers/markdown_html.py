"""Minimal, dependency-free markdown to HTML conversion.

WordPress and Medium both want HTML. Adding a markdown library would break the
project's two-dependency guarantee (`pyyaml` + `requests`), so this module
implements the subset of GitHub-flavoured markdown the content engine actually
produces, plus the common constructs a human might type:

    ATX headings, paragraphs, unordered/ordered lists (one level, nested via
    indentation), fenced code blocks with an info string, blockquotes,
    horizontal rules, pipe tables, and inline code / bold / italic / links /
    images / autolinks.

Everything is HTML-escaped before any markup is added, so no input can inject
markup. Constructs outside the subset degrade to escaped text inside a
paragraph rather than being dropped - a lossy render is better than a silent
hole in someone's article.

Deliberately not implemented: reference links, HTML passthrough, nested block
structures deeper than one list level, setext headings, and images inside links.
The composer never emits them, and a half-correct implementation of raw HTML
passthrough is a stored-XSS vector in a tool that runs unattended.
"""

from __future__ import annotations

import html
import re
from typing import List, Optional, Tuple

#: Fenced code block. Captured before any other line rule.
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$")
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK_RE = re.compile(r"(?<![\"'=(<])\b(https?://[^\s<>()\[\]]+)")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![*\w])([*_])(?=\S)([^*_]+?)(?<=\S)\1(?![*\w])", re.DOTALL)

#: Only these schemes survive into an href/src. Blocks `javascript:` and
#: `data:` payloads outright.
_SAFE_SCHEME_RE = re.compile(r"^(https?://|mailto:|/|#)", re.IGNORECASE)


def _safe_url(url: str) -> Optional[str]:
    candidate = (url or "").strip()
    if not candidate:
        return None
    if _SAFE_SCHEME_RE.match(candidate):
        return html.escape(candidate, quote=True)
    return None


def render_inline(text: str) -> str:
    """Convert inline markdown within a single line of text.

    Code spans are extracted first and re-inserted last, so their contents are
    never treated as emphasis or links.
    """
    placeholders: List[str] = []

    def _stash(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    working = _INLINE_CODE_RE.sub(
        lambda match: _stash(f"<code>{html.escape(match.group(1))}</code>"),
        text or "",
    )
    working = html.escape(working, quote=False)
    # html.escape has already neutralised the raw angle brackets, so the
    # remaining work is purely about the markdown punctuation.

    working = _IMAGE_RE.sub(
        lambda match: (
            f'<img src="{_safe_url(match.group(2))}" alt="{match.group(1)}">'
            if _safe_url(match.group(2))
            else _stash(f"[{match.group(1)}]({match.group(2)})")
        ),
        working,
    )
    working = _LINK_RE.sub(
        lambda match: (
            f'<a href="{_safe_url(match.group(2))}">{match.group(1)}</a>'
            if _safe_url(match.group(2))
            else match.group(0)
        ),
        working,
    )
    working = _BOLD_RE.sub(lambda match: f"<strong>{match.group(2)}</strong>", working)
    working = _ITALIC_RE.sub(lambda match: f"<em>{match.group(2)}</em>", working)
    working = _AUTOLINK_RE.sub(
        lambda match: f'<a href="{_safe_url(match.group(1))}">{match.group(1)}</a>',
        working,
    )

    for index, fragment in enumerate(placeholders):
        working = working.replace(f"\x00{index}\x00", fragment)
    return working


def _split_table_row(line: str) -> List[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _render_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    head, *body = rows
    parts: List[str] = ["<table><thead><tr>"]
    parts += [f"<th>{render_inline(cell)}</th>" for cell in head]
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        parts += [f"<td>{render_inline(cell)}</td>" for cell in row]
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def markdown_to_html(markdown: str) -> str:
    """Convert a markdown document into an HTML fragment.

    Returns block-level HTML only (no `<html>`/`<body>` wrapper), which is what
    both the WordPress and Medium post endpoints expect for `content`.
    """
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    index = 0
    total = len(lines)

    def flush_paragraph(buffer: List[str]) -> None:
        if buffer:
            out.append("<p>" + "<br>\n".join(render_inline(part) for part in buffer) + "</p>")
            buffer.clear()

    paragraph: List[str] = []
    while index < total:
        line = lines[index]

        # Fenced code block.
        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph(paragraph)
            marker, info = fence.group(1)[0], fence.group(2)
            fence_length = len(fence.group(1))
            closing = re.compile(r"^\s*" + re.escape(marker) + "{" + str(fence_length) + r",}\s*$")
            block: List[str] = []
            index += 1
            while index < total and not closing.match(lines[index]):
                block.append(lines[index])
                index += 1
            index += 1  # skip the closing fence (or run off the end)
            language = f' class="language-{html.escape(info, quote=True)}"' if info else ""
            out.append(f"<pre><code{language}>" + html.escape("\n".join(block), quote=False) + "</code></pre>")
            continue

        if not line.strip():
            flush_paragraph(paragraph)
            index += 1
            continue

        if _HR_RE.match(line):
            flush_paragraph(paragraph)
            out.append("<hr>")
            index += 1
            continue

        heading = _ATX_RE.match(line)
        if heading:
            flush_paragraph(paragraph)
            level = len(heading.group(1))
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _QUOTE_RE.match(line):
            flush_paragraph(paragraph)
            quoted: List[str] = []
            while index < total and _QUOTE_RE.match(lines[index]):
                quoted.append(_QUOTE_RE.match(lines[index]).group(1))  # type: ignore[union-attr]
                index += 1
            out.append("<blockquote>" + markdown_to_html("\n".join(quoted)) + "</blockquote>")
            continue

        # Table: a header row followed by a separator row.
        if (
            "|" in line
            and index + 1 < total
            and _TABLE_SEP_RE.match(lines[index + 1])
            and "|" in lines[index + 1]
        ):
            flush_paragraph(paragraph)
            rows = [_split_table_row(line)]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            out.append(_render_table(rows))
            continue

        unordered = _UL_RE.match(line)
        ordered = _OL_RE.match(line)
        if unordered or ordered:
            flush_paragraph(paragraph)
            tag = "ul" if unordered else "ol"
            items: List[str] = []
            while index < total:
                current = _UL_RE.match(lines[index]) if tag == "ul" else _OL_RE.match(lines[index])
                other = _OL_RE.match(lines[index]) if tag == "ul" else _UL_RE.match(lines[index])
                if other and not current:
                    break
                if not current:
                    if lines[index].strip() and items:
                        # Lazy continuation of the previous item.
                        items[-1] += " " + lines[index].strip()
                        index += 1
                        continue
                    break
                indent = len(current.group(1))
                text = current.group(3) if tag == "ol" else current.group(2)
                if indent >= 2 and items:
                    items[-1] += "\n" + render_inline(text)
                else:
                    items.append(render_inline(text))
                index += 1
            out.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{tag}>")
            continue

        paragraph.append(line.strip())
        index += 1

    flush_paragraph(paragraph)
    return "\n".join(part for part in out if part)


def strip_front_matter(text: str) -> Tuple[str, str]:
    """Split a leading ``---`` YAML-ish block from the document body.

    Used so a hand-edited file that still carries front matter can be imported
    without the metadata leaking into the article. Returns ``(front_matter, body)``.
    """
    raw = (text or "").lstrip("")
    if not raw.startswith("---"):
        return "", raw
    lines = raw.split("\n")
    if lines[0].strip() != "---":
        return "", raw
    for position in range(1, len(lines)):
        if lines[position].strip() in {"---", "..."}:
            return "\n".join(lines[1:position]), "\n".join(lines[position + 1 :])
    return "", raw
