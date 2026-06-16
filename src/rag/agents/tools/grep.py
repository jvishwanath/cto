import subprocess
from pathlib import Path

from langchain_core.tools import tool

from ...config import REPOS_DIR
from ._repo import resolve_repo


@tool
def grep(pattern: str, repo: str | None = None,
         file_glob: str | None = None,
         case_sensitive: bool = False) -> str:
    """Regex search across repository files using ripgrep.

    Use this for exact identifiers, error strings, config keys, or any literal
    pattern that semantic search would miss. Returns file:line matches.

    Args:
        pattern: Regex pattern to search for.
        repo: Optional — exact name of an indexed repository. Omit to search all.
        file_glob: Optional glob to restrict files (e.g., '*.java', '*.properties').
        case_sensitive: Default False (rg `-i`). Pass True for exact-case match.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    base = Path(REPOS_DIR)
    search_path = base / repo if repo else base

    if not search_path.exists():
        return f"Error: path '{search_path}' does not exist"

    cmd = ["rg", "--follow", "--line-number", "--no-heading", "--max-count", "5",
           "--max-columns", "200"]
    if not case_sensitive:
        cmd.append("-i")
    cmd.extend(["-e", pattern])
    if file_glob:
        cmd.extend(["--glob", file_glob])
    cmd.append(str(search_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return "Error: ripgrep (rg) is not installed. Install with: brew install ripgrep"
    except subprocess.TimeoutExpired:
        return f"Error: grep timed out for pattern {pattern!r}"

    output = result.stdout.strip()
    if not output:
        return f"No matches for pattern {pattern!r}" + (f" in {repo}" if repo else "")

    # Make paths relative to REPOS_DIR for readability
    lines = []
    for line in output.splitlines()[:50]:
        lines.append(line.replace(str(base) + "/", ""))

    return "\n".join(lines)
