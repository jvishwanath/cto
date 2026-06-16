"""
JIRA on-demand lookup tools — like the git tools, no index: fetch from
the JIRA REST API at query time. Resolves the ticket IDs that commits
and Confluence pages reference but the corpus can't explain.

DC auth: Personal Access Token → Authorization: Bearer.
Cloud auth: email + API token → Basic (set JIRA_USER).
No-ops with a clear message when JIRA_TOKEN is unset.
"""

from __future__ import annotations

import json
import re

import httpx
from langchain_core.tools import tool, BaseTool
from langgraph.types import interrupt

from ...config import JIRA_BASE_URL, JIRA_TOKEN, JIRA_USER, JIRA_VERIFY_SSL

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d+$")
_MAX_COMMENTS = 5
_MAX_FIELD = 1500

_client: httpx.Client | None = None


def _http() -> httpx.Client | None:
    global _client
    if not JIRA_TOKEN:
        return None
    if _client is None:
        headers = {"Accept": "application/json"}
        auth = None
        if JIRA_USER:  # Cloud Basic
            auth = (JIRA_USER, JIRA_TOKEN)
        else:          # DC Bearer PAT
            headers["Authorization"] = f"Bearer {JIRA_TOKEN}"
        _client = httpx.Client(
            base_url=f"{JIRA_BASE_URL}/rest/api/2",
            headers=headers, auth=auth,
            timeout=20.0, verify=JIRA_VERIFY_SSL,
        )
    return _client


def _adf_to_text(v) -> str:
    """DC returns plain strings; Cloud may return ADF (dict). Flatten."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        out = []
        def walk(n):
            if isinstance(n, dict):
                if n.get("type") == "text":
                    out.append(n.get("text", ""))
                for c in n.get("content", []) or []:
                    walk(c)
            elif isinstance(n, list):
                for c in n:
                    walk(c)
        walk(v)
        return " ".join(out)
    return ""


def _fmt_issue(d: dict, *, full: bool) -> dict:
    f = d.get("fields", {})
    issue = {
        "key": d.get("key"),
        "summary": f.get("summary", ""),
        "type": (f.get("issuetype") or {}).get("name"),
        "status": (f.get("status") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "reporter": (f.get("reporter") or {}).get("displayName"),
        "created": f.get("created", "")[:10],
        "updated": f.get("updated", "")[:10],
        "url": f"{JIRA_BASE_URL}/browse/{d.get('key')}",
    }
    labels = f.get("labels") or []
    if labels:
        issue["labels"] = labels
    fix = [v.get("name") for v in (f.get("fixVersions") or [])]
    if fix:
        issue["fix_versions"] = fix
    links = []
    for l in (f.get("issuelinks") or []):
        t = (l.get("type") or {})
        if l.get("outwardIssue"):
            links.append(f"{t.get('outward','rel')} {l['outwardIssue']['key']}")
        elif l.get("inwardIssue"):
            links.append(f"{t.get('inward','rel')} {l['inwardIssue']['key']}")
    if links:
        issue["linked"] = links
    if full:
        desc = _adf_to_text(f.get("description"))
        if desc:
            issue["description"] = desc[:_MAX_FIELD]
        comments = ((f.get("comment") or {}).get("comments")) or []
        if comments:
            issue["comments"] = [
                {"author": (c.get("author") or {}).get("displayName"),
                 "when": c.get("created", "")[:10],
                 "body": _adf_to_text(c.get("body"))[:600]}
                for c in comments[-_MAX_COMMENTS:]
            ]
    return issue


@tool
def jira_lookup(ticket: str) -> str:
    """Resolve a JIRA ticket to its title, status, type, assignee,
    description, linked issues, and recent comments.

    Use this whenever a question references a ticket ID (e.g.
    "PROJ-69412", "PROJ-1234") — from the user, a commit message
    (find_commits_for_jira), or a Confluence page. Tells you the WHY
    and current STATUS that code/commits alone don't.

    Args:
        ticket: JIRA key, e.g. "PROJ-69412".
    """
    ticket = ticket.strip().upper()
    if not _KEY_RE.match(ticket):
        return f"'{ticket}' is not a valid JIRA key (expected e.g. PROJ-69412)."
    c = _http()
    if c is None:
        return ("JIRA is not configured (set JIRA_TOKEN and "
                "JIRA_BASE_URL in .env).")
    try:
        r = c.get(f"/issue/{ticket}", params={
            "fields": ("summary,status,issuetype,priority,assignee,reporter,"
                       "created,updated,labels,fixVersions,issuelinks,"
                       "description,comment"),
        })
    except Exception as e:
        return f"[jira error: {type(e).__name__}: {e}]"
    if r.status_code == 404:
        return f"Ticket {ticket} not found (or no permission to view it)."
    if r.status_code >= 400:
        return f"[jira {r.status_code}: {r.text[:200]}]"
    return json.dumps(_fmt_issue(r.json(), full=True), indent=2)


@tool
def jira_search(jql: str, limit: int = 15) -> str:
    """Search JIRA with a JQL query. Use for "open bugs in project X",
    "tickets assigned to Y", "what's in the current sprint", or to find
    tickets by component/label when you don't have an ID.

    Args:
        jql: JQL, e.g. 'project = PROJ AND status = Open ORDER BY updated DESC'.
        limit: Max results (default 15, capped 50).
    """
    c = _http()
    if c is None:
        return ("JIRA is not configured (set JIRA_TOKEN and "
                "JIRA_BASE_URL in .env).")
    try:
        r = c.get("/search", params={
            "jql": jql, "maxResults": max(1, min(int(limit), 50)),
            "fields": ("summary,status,issuetype,priority,assignee,"
                       "updated,labels"),
        })
    except Exception as e:
        return f"[jira error: {type(e).__name__}: {e}]"
    if r.status_code >= 400:
        return f"[jira {r.status_code}: {r.text[:300]}]"
    data = r.json()
    issues = [_fmt_issue(i, full=False) for i in data.get("issues", [])]
    if not issues:
        return f"No issues match JQL: {jql}"
    return json.dumps({
        "jql": jql,
        "total": data.get("total", len(issues)),
        "shown": len(issues),
        "issues": issues,
    }, indent=2)


# ════════════════════════════════════════════════════════════════════
# Write tools — superdev only. Not in ALL_TOOLS / TOOLS_BY_NAME.
# Built via build_jira_write_tools() and spliced into host.py.
# ════════════════════════════════════════════════════════════════════


def _confirm_jira_write(action: str, summary: str) -> bool:
    """Pause the graph and ask the user. Requires a checkpointer
    (superdev graph has one). Returns True if the user approves."""
    ans = str(interrupt({
        "kind": "jira-write-confirm",
        "question": (
            f"⚠️  JIRA write requested ({action}):\n\n"
            f"  {summary}\n\nProceed?"),
        "options": ["yes — submit", "no — skip"],
    })).strip().lower()
    return ans.startswith(("y", "1"))


def _resolve_transition(c: httpx.Client, ticket: str,
                        transition: str) -> tuple[str | None, str | None,
                                                  str | None]:
    """Return (transition_id, transition_name, error_msg). If
    `transition` is all-digits, treat as ID and skip the lookup."""
    t = transition.strip()
    if t.isdigit():
        return t, t, None
    try:
        r = c.get(f"/issue/{ticket}/transitions")
    except Exception as e:
        return None, None, f"[jira error: {type(e).__name__}: {e}]"
    if r.status_code == 404:
        return None, None, (f"Ticket {ticket} not found "
                            f"(or no permission to view it).")
    if r.status_code >= 400:
        return None, None, f"[jira {r.status_code}: {r.text[:200]}]"
    trs = r.json().get("transitions", []) or []
    tl = t.lower()
    for tr in trs:
        if (tr.get("name") or "").lower() == tl:
            return str(tr.get("id")), tr.get("name"), None
    names = [tr.get("name") for tr in trs]
    return None, None, (
        f"No transition matching {transition!r} on {ticket}. "
        f"Available: {names}")


def build_jira_write_tools(
    *, confirm: callable | None = None,
) -> list[BaseTool]:
    """Factory returning [jira_add_comment, jira_list_transitions,
    jira_transition_issue]. Pass `confirm` to override the default
    interrupt-based gate (subagents must auto-deny with
    `lambda a, s: False` — they cannot surface interrupt())."""
    _confirm = confirm if confirm is not None else _confirm_jira_write

    @tool
    def jira_add_comment(ticket: str, body: str) -> str:
        """Post a comment on a JIRA ticket.

        WARNING: writes to JIRA. Pauses for user confirmation before
        posting. Subagents auto-deny (cannot surface the prompt).

        Args:
            ticket: JIRA key, e.g. "PROJ-69412".
            body: Comment text (plain). Empty / whitespace rejected.
        """
        ticket = ticket.strip().upper()
        if not _KEY_RE.match(ticket):
            return (f"'{ticket}' is not a valid JIRA key "
                    f"(expected e.g. PROJ-69412).")
        body = (body or "").strip()
        if not body:
            return "Refused: comment body is empty."
        c = _http()
        if c is None:
            return ("JIRA is not configured (set JIRA_TOKEN and "
                    "JIRA_BASE_URL in .env).")
        if not _confirm("add comment",
                        f"{ticket}: {body[:200]}"):
            return f"Skipped by user: comment not posted to {ticket}."
        try:
            r = c.post(f"/issue/{ticket}/comment",
                       json={"body": body})
        except Exception as e:
            return f"[jira error: {type(e).__name__}: {e}]"
        if r.status_code == 404:
            return f"Ticket {ticket} not found (or no permission)."
        if r.status_code >= 400:
            return f"[jira {r.status_code}: {r.text[:200]}]"
        d = r.json()
        return json.dumps({
            "key": ticket,
            "comment_id": d.get("id"),
            "author": (d.get("author") or {}).get("displayName"),
            "created": d.get("created", "")[:19],
            "body_preview": _adf_to_text(d.get("body"))[:200],
        }, indent=2)

    @tool
    def jira_list_transitions(ticket: str) -> str:
        """List the workflow transitions available on a JIRA ticket
        (read-only). Call this before `jira_transition_issue` when
        you don't know the exact transition name/ID for this project.

        Args:
            ticket: JIRA key, e.g. "PROJ-69412".
        """
        ticket = ticket.strip().upper()
        if not _KEY_RE.match(ticket):
            return (f"'{ticket}' is not a valid JIRA key "
                    f"(expected e.g. PROJ-69412).")
        c = _http()
        if c is None:
            return ("JIRA is not configured (set JIRA_TOKEN and "
                    "JIRA_BASE_URL in .env).")
        try:
            r = c.get(f"/issue/{ticket}/transitions")
        except Exception as e:
            return f"[jira error: {type(e).__name__}: {e}]"
        if r.status_code == 404:
            return f"Ticket {ticket} not found (or no permission)."
        if r.status_code >= 400:
            return f"[jira {r.status_code}: {r.text[:200]}]"
        trs = r.json().get("transitions", []) or []
        out = [{
            "id": tr.get("id"),
            "name": tr.get("name"),
            "to_status": (tr.get("to") or {}).get("name"),
        } for tr in trs]
        return json.dumps({"key": ticket, "transitions": out},
                          indent=2)

    @tool
    def jira_transition_issue(ticket: str, transition: str,
                              comment: str | None = None) -> str:
        """Transition a JIRA ticket to a new workflow state.

        WARNING: changes ticket status. Pauses for user confirmation
        before submitting. Call `jira_list_transitions` first if
        unsure of the transition name for this project's workflow.

        Args:
            ticket: JIRA key, e.g. "PROJ-69412".
            transition: Transition name (case-insensitive, e.g.
                "In Progress") OR numeric transition ID.
            comment: Optional comment posted atomically with the
                transition.
        """
        ticket = ticket.strip().upper()
        if not _KEY_RE.match(ticket):
            return (f"'{ticket}' is not a valid JIRA key "
                    f"(expected e.g. PROJ-69412).")
        c = _http()
        if c is None:
            return ("JIRA is not configured (set JIRA_TOKEN and "
                    "JIRA_BASE_URL in .env).")
        tid, tname, err = _resolve_transition(c, ticket, transition)
        if err:
            return err
        summary = f"{ticket} → {tname}" + (
            " (+comment)" if comment else "")
        if not _confirm("transition", summary):
            return f"Skipped by user: {ticket} not transitioned."
        payload: dict = {"transition": {"id": tid}}
        if comment:
            payload["update"] = {
                "comment": [{"add": {"body": comment}}]}
        try:
            r = c.post(f"/issue/{ticket}/transitions", json=payload)
        except Exception as e:
            return f"[jira error: {type(e).__name__}: {e}]"
        if r.status_code == 404:
            return f"Ticket {ticket} not found (or no permission)."
        if r.status_code >= 400:
            return f"[jira {r.status_code}: {r.text[:200]}]"
        return json.dumps({
            "key": ticket,
            "transitioned_to": tname,
            "comment_added": bool(comment),
        }, indent=2)

    return [jira_add_comment, jira_list_transitions,
            jira_transition_issue]
