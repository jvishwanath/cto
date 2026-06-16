"""
Fallback chunker for files without a specialized parser (YAML, JSON, SQL, Dockerfile, etc.).
Uses the same naive token-window approach as Phase 0, but with context prefix.
"""

from .code import _tokenizer

_CHUNK_SIZE = 500
_OVERLAP = 50


def chunk_fallback(filepath: str, text: str, repo: str) -> list[dict]:
    prefix = f"// File: {filepath}\n"
    tokens = _tokenizer().encode(text)

    if not tokens:
        return []

    # Small files: single chunk
    if len(tokens) <= _CHUNK_SIZE:
        return [{
            "text": prefix + text,
            "metadata": {
                "repo": repo,
                "filepath": filepath,
                "start_line": 1,
                "chunk_type": "fallback_window",
            },
        }]

    chunks = []
    start = 0
    idx = 0
    while start < len(tokens):
        end = start + _CHUNK_SIZE
        chunk_tokens = tokens[start:end]
        chunk_text = _tokenizer().decode(chunk_tokens)
        lines_before = _tokenizer().decode(tokens[:start]).count("\n")

        chunks.append({
            "text": prefix + chunk_text,
            "metadata": {
                "repo": repo,
                "filepath": filepath,
                "start_line": lines_before + 1,
                "chunk_type": "fallback_window",
                "chunk_part": idx,
            },
        })
        start += _CHUNK_SIZE - _OVERLAP
        idx += 1

    return chunks
