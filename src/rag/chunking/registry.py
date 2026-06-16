"""
ChunkerRegistry: routes files to the appropriate chunking strategy by extension.
- Code files (.java, .py, .go, .js, .ts) → tree-sitter AST chunker
- Markdown (.md) → header-hierarchy chunker
- Config/other → naive token-window fallback
"""

from pathlib import Path

from .code import chunk_code_file
from .markdown import chunk_markdown_file
from .fallback import chunk_fallback

TREE_SITTER_EXTENSIONS = {
    ".java", ".py", ".go", ".js", ".ts", ".tsx", ".jsx",
    ".rs",
    ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".c",
    ".tf", ".tfvars", ".hcl",
}

MARKDOWN_EXTENSIONS = {
    ".md", ".mdx",
}


def chunk_file(filepath: str, text: str, repo: str) -> list[dict]:
    ext = Path(filepath).suffix.lower()

    if ext in TREE_SITTER_EXTENSIONS:
        chunks = chunk_code_file(filepath, text, repo, ext)
        if chunks:
            return chunks
        return chunk_fallback(filepath, text, repo)

    if ext in MARKDOWN_EXTENSIONS:
        return chunk_markdown_file(filepath, text, repo)

    return chunk_fallback(filepath, text, repo)
