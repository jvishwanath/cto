"""
on_complete hooks (Phase 9-D).

  index     — already done by write_report → no-op confirm.
  diff_prev — unified diff of this report vs the most recent
              prior 'ok' run for the same (job, repo); writes
              <report>-delta.md alongside.
  notify    — pluggable by URI scheme. slack:// is a logger
              stub until SLACK_WEBHOOK_URL is set; log:// always
              prints. The Phase-9 checkpoint only needs the
              shape; real Slack POST is one urllib call.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from pathlib import Path
from urllib.request import Request, urlopen

from .store import prev_report

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def diff_prev(job: str, repo: str | None, run_id: int,
              report_path: str | None) -> str | None:
    if not report_path:
        return None
    cur = Path(report_path)
    if not cur.exists():
        return None
    prev = prev_report(job, repo, run_id)
    if not prev or not prev.get("report_path"):
        return None
    pp = Path(prev["report_path"])
    if not pp.exists():
        return None
    a = pp.read_text(encoding="utf-8").splitlines(keepends=True)
    b = cur.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        a, b, fromfile=pp.name, tofile=cur.name, n=2))
    if not diff:
        body = "_(no changes since previous run)_\n"
    else:
        added = sum(1 for ln in diff
                    if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff
                      if ln.startswith("-") and not ln.startswith("---"))
        body = (f"# Δ {job} — {repo or ''}\n\n"
                f"+{added} / −{removed} lines vs "
                f"`{pp.name}`\n\n```diff\n"
                + "".join(diff)[:50_000] + "\n```\n")
    out = cur.with_name(cur.stem + "-delta.md")
    out.write_text(body, encoding="utf-8")
    log.info("[hook diff_prev] %s → %s", job, out)
    return str(out)


def notify(target: str | None, title: str, body: str) -> bool:
    if not target:
        return False
    scheme, _, rest = target.partition("://")
    text = f"*{title}*\n{body[:2800]}"
    if scheme == "slack":
        if not SLACK_WEBHOOK_URL:
            log.info("[notify slack→%s] (SLACK_WEBHOOK_URL unset — "
                     "would post)\n%s", rest or "default", text)
            return True
        try:
            req = Request(
                SLACK_WEBHOOK_URL,
                data=json.dumps({"text": text,
                                 "channel": rest or None}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            with urlopen(req, timeout=10) as r:
                ok = r.status < 300
            log.info("[notify slack→%s] %s", rest, "ok" if ok else r.status)
            return ok
        except Exception as e:
            log.warning("[notify slack] failed: %s", e)
            return False
    if scheme in ("log", ""):
        log.info("[notify %s] %s\n%s", rest or "-", title, body)
        return True
    log.warning("[notify] unsupported scheme %r", scheme)
    return False
