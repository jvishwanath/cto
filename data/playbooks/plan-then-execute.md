**Plan-Then-Execute — no edits before a checklist exists**

Before touching code:

1. **Understand** — `find_callers`/`find_callees`/`search_code`
   on the symbols you'll change. List every call site across
   ALL indexed repos, not just this worktree.
2. **Plan** — `todo(set, items=[…])` with 3–8 concrete steps,
   each ≤5 min, each independently verifiable. Include the
   file paths. **Your LAST two items MUST be `commit changes`
   and `open PR` — they are part of the plan, not an
   afterthought.** `ask_user` to confirm the plan if it
   touches >3 files or any public interface.
3. **Execute** — one item at a time. After each:
   `host_shell(<verify command>)` → `todo(done, "<n>")`.
   Do not start item N+1 until N is verified. **Do NOT mark
   an item done if its verify command exited non-zero** —
   either fix it, or `ask_user` how to proceed (e.g. build
   tooling missing). A skipped verification is a lie.
4. **Wrap** — `host_shell("git diff --stat")` →
   `commit(kind, area, desc)` → `todo(done, "commit changes")`
   → `ask_user("Open a PR?")` → if yes,
   `create_pr(title, body)` → `todo(done, "open PR")`.
   **You are NOT done until `create_pr` returned a URL or the
   user explicitly said no. Do NOT write a final summary
   before this step completes.**

If scope creeps mid-execution, STOP, `todo(add, "<new item>")`,
re-confirm with `ask_user` if the plan grew >50%.
