"""
CodeExecutor subgraph: write_code → run_sandbox → check → (fix_code → run_sandbox)*.
Own state, own loop bound (MAX_ATTEMPTS), own safety-focused system prompt,
own restricted toolset (sandbox only). Invoked via the execute_code tool.
"""

import re
from typing import TypedDict

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END

from ..llm import llm
from ...sandbox import DockerSandbox, SandboxResult

MAX_ATTEMPTS = 4

_WRITE_SYSTEM = """You write a single self-contained {lang} script to
accomplish the user's task inside an isolated sandbox.

Sandbox environment:
- No network access. No package installation.
- The repository (if provided) is mounted read-only at /repo.
- Working directory is /work (read-only). /tmp is writable (64MB, noexec).
- Available Python libs: stdlib, regex, requests (will fail — no network),
  pytest, pyyaml.
- Timeout: 30 seconds. Memory: 512MB.

Rules:
- Output ONLY the code inside one fenced block: ```{lang} ... ```
- Print results to stdout. Use assertions for verification tasks.
- Do NOT attempt to: import os to spawn processes, open sockets, write
  outside /tmp, read environment variables, or escape the sandbox.
- Keep it minimal — under 60 lines.
"""

_FIX_SYSTEM = """The previous attempt failed. Diagnose the error from
stderr and produce a corrected {lang} script.

Sandbox constraints (unchanged): no network, /repo read-only, /tmp writable,
30s timeout. Output ONLY one fenced ```{lang}``` code block.
"""

_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)


class ExecutorState(TypedDict, total=False):
    task: str
    repo: str | None
    lang: str
    code: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    attempt: int
    duration_ms: int


_executor_llm = llm("agent", temperature=0)
_sandbox: DockerSandbox | None = None


def _get_sandbox() -> DockerSandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = DockerSandbox()
    return _sandbox


def _extract_code(text: str) -> str:
    m = _CODE_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # Model emitted raw code without fences — take it as-is.
    return text.strip()


# ── Nodes ────────────────────────────────────────────────────────────

def write_code(state: ExecutorState) -> dict:
    lang = state.get("lang", "python")
    repo = state.get("repo")
    repo_hint = f"\nRepository '{repo}' is mounted at /repo." if repo else ""

    response = _executor_llm.invoke([
        SystemMessage(content=_WRITE_SYSTEM.format(lang=lang)),
        HumanMessage(content=f"Task: {state['task']}{repo_hint}"),
    ])
    return {"code": _extract_code(response.content), "attempt": 1}


def run_sandbox(state: ExecutorState) -> dict:
    result: SandboxResult = _get_sandbox().run(
        code=state["code"],
        lang=state.get("lang", "python"),
        repo=state.get("repo"),
        timeout=30,
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
    }


def check_result(state: ExecutorState) -> str:
    if state.get("exit_code") == 0 and not state.get("timed_out"):
        return "done"
    if state.get("attempt", 1) >= MAX_ATTEMPTS:
        return "done"
    return "retry"


def fix_code(state: ExecutorState) -> dict:
    lang = state.get("lang", "python")
    response = _executor_llm.invoke([
        SystemMessage(content=_FIX_SYSTEM.format(lang=lang)),
        HumanMessage(content=(
            f"Task: {state['task']}\n\n"
            f"Previous code:\n```{lang}\n{state['code']}\n```\n\n"
            f"exit_code: {state.get('exit_code')}  timed_out: {state.get('timed_out')}\n"
            f"stderr:\n{state.get('stderr','')}\n\n"
            f"stdout:\n{state.get('stdout','')}\n\n"
            f"Provide the corrected script."
        )),
    ])
    return {
        "code": _extract_code(response.content),
        "attempt": state.get("attempt", 1) + 1,
    }


# ── Graph assembly ──────────────────────────────────────────────────

def build_executor():
    g = StateGraph(ExecutorState)
    g.add_node("write_code", write_code)
    g.add_node("run_sandbox", run_sandbox)
    g.add_node("fix_code", fix_code)

    g.add_edge(START, "write_code")
    g.add_edge("write_code", "run_sandbox")
    g.add_conditional_edges("run_sandbox", check_result,
                            {"done": END, "retry": "fix_code"})
    g.add_edge("fix_code", "run_sandbox")

    # No checkpointer: runs are short and idempotent (fresh container each time).
    return g.compile()


_executor_app = None


def get_executor():
    global _executor_app
    if _executor_app is None:
        _executor_app = build_executor()
    return _executor_app
