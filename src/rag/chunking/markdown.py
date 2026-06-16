"""
Header-hierarchy markdown chunker.
Splits on ## / ### headings, prepends breadcrumb path for context.
"""

import re

from .code import _tokenizer

_MAX_CHUNK_TOKENS = 1000

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)


def chunk_markdown_file(filepath: str, text: str, repo: str) -> list[dict]:
    sections = _split_by_headings(text)

    if not sections:
        return [{
            "text": text[:4000],
            "metadata": {
                "repo": repo,
                "filepath": filepath,
                "start_line": 1,
                "chunk_type": "markdown_full",
                "section_path": "",
            },
        }]

    chunks = []
    for section in sections:
        breadcrumb = " > ".join(section["heading_stack"])
        enriched = f"// Path: {filepath} | Section: {breadcrumb}\n{section['text']}"

        tokens = _tokenizer().encode(enriched)
        if len(tokens) > _MAX_CHUNK_TOKENS:
            # Split long sections into overlapping sub-chunks
            sub_chunks = _split_long_section(enriched, filepath, repo, section, breadcrumb)
            chunks.extend(sub_chunks)
        else:
            chunks.append({
                "text": enriched,
                "metadata": {
                    "repo": repo,
                    "filepath": filepath,
                    "start_line": section["start_line"],
                    "chunk_type": "markdown_section",
                    "section_path": breadcrumb,
                },
            })

    return chunks


def _split_by_headings(text: str) -> list[dict]:
    lines = text.split("\n")
    sections = []
    heading_stack = []
    current_lines = []
    current_start = 1

    for i, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match:
            # Flush current section
            if current_lines:
                section_text = "\n".join(current_lines).strip()
                if section_text:
                    sections.append({
                        "text": section_text,
                        "heading_stack": list(heading_stack),
                        "start_line": current_start,
                    })

            level = len(match.group(1))
            title = match.group(2).strip()

            # Maintain heading hierarchy
            heading_stack = heading_stack[:level - 1]
            heading_stack.append(title)

            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    # Flush final section
    if current_lines:
        section_text = "\n".join(current_lines).strip()
        if section_text:
            sections.append({
                "text": section_text,
                "heading_stack": list(heading_stack),
                "start_line": current_start,
            })

    return sections


def _split_long_section(text: str, filepath: str, repo: str, section: dict, breadcrumb: str) -> list[dict]:
    tokens = _tokenizer().encode(text)
    chunks = []
    start = 0
    idx = 0
    overlap = 50

    while start < len(tokens):
        end = start + _MAX_CHUNK_TOKENS
        chunk_text = _tokenizer().decode(tokens[start:end])

        chunks.append({
            "text": chunk_text,
            "metadata": {
                "repo": repo,
                "filepath": filepath,
                "start_line": section["start_line"],
                "chunk_type": "markdown_section",
                "section_path": breadcrumb,
                "chunk_part": idx,
            },
        })
        start = end - overlap
        idx += 1

    return chunks
