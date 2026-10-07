"""Convert the Markdown subset agents produce into Telegram HTML.

Only unambiguous constructs are converted: fenced code blocks, inline code,
`**bold**`, `~~strike~~`, `[text](http...)` links, headings and list bullets.
Single `*`/`_` emphasis is left as-is so snake_case names and globs survive.
Everything else is HTML-escaped.
"""

from __future__ import annotations

import html
import re


FENCE_RE = re.compile(r"^\s*```")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
BULLET_RE = re.compile(r"^(\s*)[-*]\s+")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def markdown_to_telegram_html(text: str) -> str:
    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            if in_code:
                out.append("</code></pre>")
            else:
                out.append("<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line, quote=False))
            continue
        heading = HEADING_RE.match(line)
        if heading:
            out.append(f"<b>{format_inline(heading.group(1))}</b>")
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            line = f"{bullet.group(1)}• {line[bullet.end():]}"
        out.append(format_inline(line))
    if in_code:
        out.append("</code></pre>")

    # Code block tags were emitted as separate lines; glue them to their content
    # so Telegram does not render stray blank lines around the block.
    rendered = "\n".join(out)
    rendered = rendered.replace("<pre><code>\n", "<pre><code>")
    return rendered.replace("\n</code></pre>", "</code></pre>")


def format_inline(line: str) -> str:
    parts: list[str] = []
    position = 0
    for match in INLINE_CODE_RE.finditer(line):
        parts.append(format_plain(line[position : match.start()]))
        parts.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        position = match.end()
    parts.append(format_plain(line[position:]))
    return "".join(parts)


def format_plain(text: str) -> str:
    links: list[str] = []

    def keep_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        links.append(f'<a href="{url}">{label}</a>')
        return f"\x00{len(links) - 1}\x00"

    text = LINK_RE.sub(keep_link, text)
    text = html.escape(text, quote=False)
    text = BOLD_RE.sub(r"<b>\1</b>", text)
    text = STRIKE_RE.sub(r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda match: links[int(match.group(1))], text)


def balance_code_fences(chunks: list[str]) -> list[str]:
    """Close a code fence at a chunk boundary and reopen it in the next chunk."""
    balanced: list[str] = []
    reopen = False
    for chunk in chunks:
        if reopen:
            chunk = "```\n" + chunk
        fences = sum(1 for line in chunk.split("\n") if FENCE_RE.match(line))
        reopen = fences % 2 == 1
        if reopen:
            chunk = chunk + "\n```"
        balanced.append(chunk)
    return balanced
