import json

from langchain_core.tools import tool

from ...sandbox import DockerSandbox
from ..subgraphs.code_executor import get_executor
from ._repo import resolve_repo


@tool
def execute_code(task: str, repo: str | None = None, lang: str = "python") -> str:
    """Execute code in an isolated sandbox to VERIFY behavior — not to explain it.

    Use this for: 'does X return Y for input Z', 'does this regex match ...',
    'run a quick check on the value of ...', 'verify this assertion'.
    Do NOT use for: explaining what code does (use read_file), finding code
    (use search_code/grep), or anything answerable by reading.

    The sandbox writes a script for the task, runs it (no network, read-only
    repo at /repo, 30s timeout, 512MB), and retries up to 4× on failure.

    Args:
        task: Plain-English description of what to verify/compute.
        repo: Optional repo to mount read-only at /repo inside the sandbox.
        lang: 'python' (default) or 'bash'.
    """
    repo, err = resolve_repo(repo)
    if err:
        return err
    if not DockerSandbox.available():
        return ("Error: Docker is not available on this host. "
                "Cannot execute code; answer using read_file/search_code instead.")

    result = get_executor().invoke({
        "task": task,
        "repo": repo,
        "lang": lang,
        "attempt": 0,
    })

    return json.dumps({
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out", False),
        "attempts": result.get("attempt"),
        "duration_ms": result.get("duration_ms"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "code_executed": result.get("code", ""),
    }, indent=2)
