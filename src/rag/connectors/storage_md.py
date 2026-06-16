"""
Confluence storage-format (XHTML + <ac:*> macros) → Markdown.

Storage format is the canonical body representation in both Cloud and
Data Center. It's well-formed XML; we walk it with ElementTree rather
than regex so nested macros (code inside expand inside panel) survive.
"""

from __future__ import annotations
import html
import re
import xml.etree.ElementTree as ET

# Storage format uses two namespaces but Confluence emits them WITHOUT
# xmlns declarations on the body fragment. Wrap with declarations so
# ET parses qualified names.
_NS = {
    "ac": "http://atlassian.com/content",
    "ri": "http://atlassian.com/resource",
}
_WRAP = (
    '<root xmlns:ac="http://atlassian.com/content" '
    'xmlns:ri="http://atlassian.com/resource">{}</root>'
)

_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4",
               "h5", "h6", "pre", "blockquote", "table"}


def _q(prefix: str, tag: str) -> str:
    return f"{{{_NS[prefix]}}}{tag}"


def storage_to_markdown(storage: str) -> str:
    """Convert a storage-format body string to Markdown."""
    if not storage or not storage.strip():
        return ""
    try:
        root = ET.fromstring(_WRAP.format(storage))
    except ET.ParseError:
        return _strip_tags(storage)

    out: list[str] = []
    _walk(root, out, ctx={"list": []})
    md = "".join(out)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md + "\n"


def _walk(el: ET.Element, out: list[str], ctx: dict) -> None:
    for child in el:
        _emit(child, out, ctx)
        if child.tail:
            out.append(child.tail)


def _emit(el: ET.Element, out: list[str], ctx: dict) -> None:
    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
    ns = el.tag.split("}")[0][1:] if "}" in el.tag else ""

    # ── Atlassian macros ────────────────────────────────────────────
    if ns == _NS["ac"]:
        if tag == "structured-macro":
            _macro(el, out, ctx)
            return
        if tag == "link":
            _link(el, out, ctx)
            return
        if tag == "image":
            ri = el.find(_q("ri", "attachment"))
            name = ri.get(_q("ri", "filename")) if ri is not None else "image"
            out.append(f"![{name}]")
            return
        if tag == "task-list":
            for task in el.findall(_q("ac", "task")):
                body = task.find(_q("ac", "task-body"))
                status = task.find(_q("ac", "task-status"))
                mark = "x" if (status is not None
                               and (status.text or "") == "complete") else " "
                out.append(f"- [{mark}] {_inner_text(body)}\n")
            out.append("\n")
            return
        if tag in ("emoticon", "placeholder", "adf-extension",
                   "layout", "layout-section", "layout-cell"):
            _walk(el, out, ctx)
            return
        if tag in ("plain-text-body", "rich-text-body", "parameter"):
            _walk_text(el, out, ctx)
            return
        _walk(el, out, ctx)
        return

    # ── HTML ────────────────────────────────────────────────────────
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        out.append("\n" + "#" * int(tag[1]) + " " + _inner_text(el) + "\n\n")
        return
    if tag == "p":
        out.append(_inner_md(el, ctx) + "\n\n")
        return
    if tag == "br":
        out.append("  \n")
        return
    if tag in ("strong", "b"):
        out.append(f"**{_inner_md(el, ctx)}**")
        return
    if tag in ("em", "i"):
        out.append(f"*{_inner_md(el, ctx)}*")
        return
    if tag == "code":
        out.append(f"`{_inner_text(el)}`")
        return
    if tag == "pre":
        out.append(f"\n```\n{_inner_text(el)}\n```\n\n")
        return
    if tag == "a":
        href = el.get("href", "")
        out.append(f"[{_inner_md(el, ctx)}]({href})")
        return
    if tag in ("ul", "ol"):
        ctx["list"].append(tag)
        _walk(el, out, ctx)
        ctx["list"].pop()
        out.append("\n")
        return
    if tag == "li":
        depth = max(0, len(ctx["list"]) - 1)
        marker = "- " if (ctx["list"] and ctx["list"][-1] == "ul") else "1. "
        out.append("  " * depth + marker + _inner_md(el, ctx).strip() + "\n")
        return
    if tag == "blockquote":
        body = _inner_md(el, ctx).strip()
        out.append("\n" + "\n".join(f"> {ln}" for ln in body.splitlines())
                   + "\n\n")
        return
    if tag == "table":
        _table(el, out, ctx)
        return
    if tag == "hr":
        out.append("\n---\n\n")
        return

    _walk_text(el, out, ctx)


def _macro(el: ET.Element, out: list[str], ctx: dict) -> None:
    name = el.get(_q("ac", "name"), "")
    params = {
        p.get(_q("ac", "name"), ""): (p.text or "")
        for p in el.findall(_q("ac", "parameter"))
    }
    plain = el.find(_q("ac", "plain-text-body"))
    rich = el.find(_q("ac", "rich-text-body"))

    if name in ("code", "codeblock"):
        lang = params.get("language", "")
        body = (plain.text or "") if plain is not None else _inner_text(rich)
        out.append(f"\n```{lang}\n{body.rstrip()}\n```\n\n")
        return
    if name in ("info", "note", "tip", "warning", "panel"):
        title = params.get("title", "") or name.upper()
        body = _inner_md(rich, ctx) if rich is not None else ""
        out.append(f"\n> **{title}**\n>\n"
                   + "\n".join(f"> {ln}" for ln in body.strip().splitlines())
                   + "\n\n")
        return
    if name in ("expand", "details"):
        title = params.get("title", "Details")
        body = _inner_md(rich, ctx) if rich is not None else ""
        out.append(f"\n### {title}\n\n{body}\n\n")
        return
    if name == "toc":
        return
    if name in ("jira", "jiraissues"):
        key = params.get("key") or params.get("jqlQuery", "")
        out.append(f"[JIRA: {key}]")
        return
    if name == "status":
        out.append(f"`[{params.get('title','')}]`")
        return
    if name in ("anchor", "viewfile", "drawio", "gliffy",
                "include", "excerpt-include"):
        out.append(f"[{name}: {params.get('name') or params.get('title','')}]")
        return

    body = _inner_md(rich, ctx) if rich is not None else (
        (plain.text or "") if plain is not None else "")
    if body.strip():
        out.append(body + "\n\n")


def _link(el: ET.Element, out: list[str], ctx: dict) -> None:
    body = el.find(_q("ac", "plain-text-link-body"))
    if body is None:
        body = el.find(_q("ac", "link-body"))
    text = _inner_text(body) if body is not None else ""
    page = el.find(_q("ri", "page"))
    url = el.find(_q("ri", "url"))
    user = el.find(_q("ri", "user"))
    if page is not None:
        title = page.get(_q("ri", "content-title"), "")
        out.append(f"[{text or title}]")
    elif url is not None:
        href = url.get(_q("ri", "value"), "")
        out.append(f"[{text or href}]({href})")
    elif user is not None:
        out.append(f"@{text or 'user'}")
    else:
        out.append(text)


def _table(el: ET.Element, out: list[str], ctx: dict) -> None:
    rows = el.findall(".//tr")
    if not rows:
        return
    out.append("\n")
    for i, tr in enumerate(rows):
        cells = tr.findall("./th") + tr.findall("./td")
        line = " | ".join(
            _inner_md(c, ctx).strip().replace("\n", " ").replace("|", "\\|")
            for c in cells
        )
        out.append(f"| {line} |\n")
        if i == 0:
            out.append("|" + "---|" * len(cells) + "\n")
    out.append("\n")


def _inner_md(el: ET.Element | None, ctx: dict) -> str:
    if el is None:
        return ""
    buf: list[str] = []
    if el.text:
        buf.append(el.text)
    _walk(el, buf, ctx)
    return "".join(buf)


def _walk_text(el: ET.Element, out: list[str], ctx: dict) -> None:
    if el.text:
        out.append(el.text)
    _walk(el, out, ctx)


def _inner_text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext())


def _strip_tags(s: str) -> str:
    s = re.sub(r"<ac:[^>]+>|</ac:[^>]+>|<ri:[^>]+/?>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)
