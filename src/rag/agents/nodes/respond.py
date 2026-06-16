"""
Respond node: extract [SOURCE_N] citations from the answer.

Two strategies:
- simple route: chunks were numbered [SOURCE_0..N] in the prompt → index
  retrieved_chunks by N.
- agent route: model writes its own footnote block (`[SOURCE_N]: path`)
  → parse that. Indexing retrieved_chunks would be wrong because the
  model's numbering is independent of search-result order.
"""

import re

from ..state import AgentState

_INLINE_RE = re.compile(r"\[SOURCE_(\d+)\]")
_FOOTNOTE_RE = re.compile(r"^\[SOURCE_(\d+)\]:\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)


def respond(state: AgentState) -> dict:
    answer = state.get("answer", "")
    chunks = state.get("retrieved_chunks", [])
    route = state.get("route", "agent")

    citations: list[dict] = []
    seen: set[int] = set()

    # Strategy 1: parse model-written footnotes (agent route)
    footnotes = {int(n): path.strip() for n, path in _FOOTNOTE_RE.findall(answer)}

    # Build a path → {url, title} map from retrieved chunks so footnote
    # paths can be enriched into clickable links (Confluence, web).
    by_path: dict[str, dict] = {}
    for c in chunks:
        if not c.get("url"):
            continue
        for key in (
            c.get("filepath"),
            c.get("file"),
            f"{c.get('repo','')}/{c.get('filepath','')}".strip("/"),
            c.get("url"),
        ):
            if key:
                by_path[key] = {"url": c["url"],
                                "title": c.get("title") or c.get("filepath", "")}

    refs = _INLINE_RE.findall(answer)

    for ref in refs:
        idx = int(ref)
        if idx in seen:
            continue
        seen.add(idx)

        if footnotes and idx in footnotes:
            fp = footnotes[idx]
            cite = {"ref": f"SOURCE_{idx}", "file": fp,
                    "symbol": "", "snippet": ""}
            if fp.startswith(("http://", "https://")):
                cite["url"] = fp
                hit = by_path.get(fp)
                cite["title"] = (hit or {}).get("title") or fp
            else:
                hit = by_path.get(fp) or next(
                    (v for k, v in by_path.items()
                     if k in fp or fp in k), None)
                if hit:
                    cite["url"] = hit["url"]
                    cite["title"] = hit["title"]
            citations.append(cite)
        elif route == "simple" and idx < len(chunks):
            c = chunks[idx]
            citations.append({
                "ref": f"SOURCE_{idx}",
                "file": c.get("file") or f"{c.get('repo','')}/{c.get('filepath','')}:{c.get('start_line','')}",
                "symbol": c.get("symbol") or c.get("symbol_name", ""),
                "snippet": (c.get("snippet") or c.get("text", ""))[:200],
                **({"url": c["url"], "title": c.get("title", "")}
                   if c.get("url") else {}),
            })
        # else: agent route with no footnote for this index → skip rather than mismap

    return {"citations": citations}
