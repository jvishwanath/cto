from pathlib import Path

from langchain_core.tools import tool

from ...config import REPOS_DIR
from ._repo import resolve_repo


@tool
def read_file(repo: str, path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read raw file content directly from a repo (bypasses the search index).

    Use this when you need exact, complete code — typically after search_code
    or grep has identified the file. Returns the file with line numbers.

    Args:
        repo: Exact name of an indexed repository.
        path: File path relative to repo root.
        start_line: First line to return (1-indexed). 0 = from beginning.
        end_line: Last line to return (inclusive). 0 = to end.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    base = Path(REPOS_DIR)

    # Prevent path traversal in the *input* (don't follow symlinks via resolve()
    # since data/repos/* may be symlinked to real repo locations).
    if ".." in Path(path).parts or Path(path).is_absolute() or ".." in Path(repo).parts:
        return f"Error: invalid path '{repo}/{path}'"

    fpath = base / repo / path

    if not fpath.exists():
        # Try to be helpful: suggest similar files
        candidates = list((base / repo).rglob(Path(path).name)) if (base / repo).exists() else []
        if candidates:
            suggestions = "\n".join(f"  - {c.relative_to(base / repo)}" for c in candidates[:5])
            return f"Error: file not found at '{path}'. Similar files in {repo}:\n{suggestions}"
        return f"Error: file '{path}' not found in repo '{repo}'"

    try:
        lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        return f"Error reading file: {e}"

    if start_line > 0 or end_line > 0:
        s = max(start_line - 1, 0)
        e = end_line if end_line > 0 else len(lines)
        lines = lines[s:e]
        offset = s
    else:
        offset = 0

    if len(lines) > 400:
        lines = lines[:400]
        truncated = True
    else:
        truncated = False

    numbered = "\n".join(f"{i + offset + 1:5d} | {line}" for i, line in enumerate(lines))
    if truncated:
        numbered += "\n... (truncated, use start_line/end_line to read more)"

    return f"# {repo}/{path}\n{numbered}"
