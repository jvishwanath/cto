"""
Intro / onboarding route. Catches small-talk-style "what can I do
here / how do I start" questions and answers with a dynamic welcome:
this instance's actual indexed sources + one runnable example per
capability. No retrieval, no LLM.
"""

import re

from langchain_core.messages import AIMessage

from ..state import AgentState
from ..tools._repo import available_repos
from ...config import JIRA_TOKEN

INTRO_RE = re.compile(
    r"^\s*(?:hi|hello|hey)?[\s,!.]*"
    r"(?:"
    r"  (?:what|how)\s+(?:do|can|should)\s+i\s+"
    r"      (?:do|use|ask|start|get\s+start(?:ed)?|begin)"
    r"      (?:\s+(?:here|this|with\s+this|you))?"
    r"| how\s+to\s+(?:get\s+start(?:ed)?|start|begin|"
    r"      use\s+(?:this|you))(?:\s+here)?"
    r"| (?:get|getting)\s+(?:me\s+)?start(?:ed)?"
    r"| help\s+me\s+(?:get\s+)?start(?:ed)?"
    r"| (?:show|tell)\s+me\s+(?:what|how)\s+"
    r"      (?:you\s+can\s+do|to\s+(?:start|use\s+(?:you|this)))"
    r"| what\s+(?:can|do)\s+you\s+do(?:\s+here)?"
    r"| what(?:'?s|\s+is)\s+this(?:\s+(?:tool|thing|app|for))?"
    r"| who\s+are\s+you"
    r"| how\s+(?:does\s+this|do\s+i\s+use\s+(?:you|this))(?:\s+work)?"
    r"| (?:give\s+me\s+)?(?:an?\s+)?(?:intro(?:duction)?|overview|tour)"
    r"      (?:\s+(?:to\s+(?:this|cto)|of\s+this))?"
    r"| (?:any\s+)?examples?\s*"
    r"      (?:of\s+(?:queries|questions|what\s+(?:to|i\s+can)\s+ask))?"
    r")"
    r"\s*[?!.]*\s*$",
    re.IGNORECASE | re.VERBOSE,
)


def is_intro_query(q: str) -> bool:
    return bool(INTRO_RE.match((q or "").strip()))


def _confluence_spaces() -> list[str]:
    try:
        from ...connectors.confluence import sync_status
        return [r["space_key"] for r in (sync_status() or [])]
    except Exception:
        return []


def _local_docs_count() -> int:
    try:
        from ...ingest.docs import list_docs
        return sum(1 for d in list_docs()
                   if not d["filepath"].startswith("confluence/"))
    except Exception:
        return 0


def _ex(repos: list[str], pattern: str) -> str:
    """Fill {repo}/{repo2} placeholders with real indexed names so
    the example is copy-pasteable for THIS instance."""
    a = repos[0] if repos else "<repo>"
    b = repos[1] if len(repos) > 1 else (repos[0] if repos else "<repo-b>")
    return pattern.format(repo=a, repo2=b)


def _build(repos: list[str], spaces: list[str], n_docs: int) -> str:
    L: list[str] = []
    L.append("## Code / Trace / Origin")
    L.append("")
    L.append("I answer questions about your indexed code, docs, and "
             "wiki — with citations to the exact file and line.")
    L.append("")

    L.append("**What's loaded right now**")
    L.append("")
    if repos:
        shown = ", ".join(f"`{r}`" for r in repos[:6])
        more = f" _(+{len(repos)-6} more)_" if len(repos) > 6 else ""
        L.append(f"- **Repos** ({len(repos)}): {shown}{more}")
    else:
        L.append("- **Repos**: _(none yet — drop clones into "
                 "`data/repos/`)_")
    if n_docs:
        L.append(f"- **Local docs**: {n_docs} file(s) under `data/docs/`")
    if spaces:
        L.append(f"- **Confluence**: "
                 f"{', '.join(f'`{s}`' for s in spaces)}")
    L.append("- **Git history** (live, no index): `log` · `blame` · "
             "`show` · ticket↔commit cross-ref")
    if JIRA_TOKEN:
        L.append("- **JIRA** (live): lookup any ticket; search by JQL")
    L.append("- **Other tools**: code call-graph, sandboxed code-exec, "
             "web search")
    try:
        from ...skills import list_skills
        sks = list_skills()
        if sks:
            names = ", ".join(f"`/{s.name}`" for s in sks)
            L.append(f"- **Skills** (multi-step playbooks): {names}")
    except Exception:
        pass
    L.append("")

    L.append("**Try one of these**")
    L.append("")
    L.append("| Ask… | What it exercises |")
    L.append("|---|---|")
    L.append(f"| `where is <SymbolName> defined?` | code graph "
             f"— defs/refs across repos |")
    L.append(f"| `who calls <SymbolName>?` | call graph — recursive "
             f"callers/callees |")
    L.append(f"| `{_ex(repos, 'explain the auth flow in {repo}')}` | "
             f"semantic code search + summarize |")
    if len(repos) > 1:
        L.append(f"| `{_ex(repos, 'compare JWT handling in {repo} vs {repo2}')}` "
                 f"| deep-research — parallel per-repo fan-out |")
    if spaces or n_docs:
        L.append(f"| `find the enrollment runbook` | docs/Confluence "
                 f"cascade (code → local docs → wiki) |")
    L.append(f"| `{_ex(repos, 'who last changed the auth config in {repo} and why?')}` "
             f"| **git** blame → log → show diff |")
    L.append(f"| `{_ex(repos, 'show me the last 5 commits in {repo}')}` "
             f"| **git** log (live, not indexed) |")
    if JIRA_TOKEN:
        L.append(f"| `what does PROJ-1234 cover and which commits "
                 f"reference it?` | **JIRA** lookup + cross-repo "
                 f"git grep |")
        L.append(f"| `find open JIRA tickets about enrollment "
                 f"timeouts` | **JIRA** JQL search |")
    L.append(f"| `what repos are indexed here?` | inventory / meta |")
    L.append("")

    L.append("**Tips**")
    L.append("- Repo names can be shorthand — I'll ask if it's "
             "ambiguous.")
    L.append("- Prefix `q:` for a one-liner, `detail:` for the long "
             "answer; or just say *more* to expand the previous reply.")
    L.append("- Follow-ups keep context — \"and where is *that* "
             "called?\" works.")
    L.append("- I cite every claim; click a `[N]` to see the source.")
    return "\n".join(L)


def intro(state: AgentState) -> dict:
    repos = available_repos(refresh=True)
    text = _build(repos, _confluence_spaces(), _local_docs_count())
    return {
        "route": "intro",
        "answer": text,
        "citations": [],
        "retrieved_chunks": [],
        "messages": [AIMessage(content=text)],
    }
