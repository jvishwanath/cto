"""
PDF → markdown extraction via PyMuPDF.

Strategy: extract text spans with font size; treat the largest sizes
as headings (## / ###) so the existing markdown chunker produces
section-aware chunks. Each output line carries the originating page.
"""

from collections import Counter
from pathlib import Path

import fitz  # pymupdf


def pdf_to_markdown(path: str | Path) -> tuple[str, dict[int, int]]:
    """
    Returns (markdown_text, line_to_page) where line_to_page maps
    1-indexed output line numbers → PDF page numbers.
    """
    doc = fitz.open(str(path))

    # First pass: collect all font sizes to determine heading thresholds.
    sizes: Counter[float] = Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    sizes[round(span["size"], 1)] += len(span["text"])

    if not sizes:
        return "", {}

    # Body size = the most common size by character count. Anything
    # ≥1.4× body is H2; ≥1.15× is H3.
    body_size = sizes.most_common(1)[0][0]
    h2_threshold = body_size * 1.4
    h3_threshold = body_size * 1.15

    md_lines: list[str] = []
    line_to_page: dict[int, int] = {}

    def emit(text: str, page_num: int):
        md_lines.append(text)
        line_to_page[len(md_lines)] = page_num

    for page_num, page in enumerate(doc, start=1):
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                spans = line["spans"]
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                max_size = max(s["size"] for s in spans)

                if max_size >= h2_threshold and len(text) < 120:
                    emit("", page_num)
                    emit(f"## {text}", page_num)
                    emit("", page_num)
                elif max_size >= h3_threshold and len(text) < 120:
                    emit("", page_num)
                    emit(f"### {text}", page_num)
                    emit("", page_num)
                else:
                    emit(text, page_num)

        emit("", page_num)

    doc.close()
    return "\n".join(md_lines), line_to_page


def page_for_line(line_to_page: dict[int, int], line: int) -> int:
    """Find the page for a given output line (or nearest preceding)."""
    if line in line_to_page:
        return line_to_page[line]
    for ln in sorted(line_to_page.keys(), reverse=True):
        if ln <= line:
            return line_to_page[ln]
    return 1
