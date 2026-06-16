**Subagent-Driven Development — one fresh agent per step**

You are the *orchestrator*. You plan, dispatch, review, and
integrate. You do NOT implement — subagents do, each with a
clean context so prior missteps don't contaminate the next
step.

1. **Plan** — `find_callers`/`search_code` to scope the
   change, then `todo(set, items=[…, "commit changes",
   "open PR"])`. Each item must be self-contained: a
   subagent with NO history must be able to execute it.

2. **Dispatch** — for each non-commit item:
   ```
   result = spawn(
     task = "<the todo item, verbatim>",
     context = "<file paths + relevant findings from step 1
                — the child has no other context>")
   ```
   For N items that touch **independent** files, batch them:
   ```
   spawn_parallel(tasks=[item1, item2, …],
                  context="<shared context>")
   ```
   (`commit` your own WIP first — `spawn_parallel` refuses a
   dirty worktree.)

3. **Review (two-stage)** — for each result:
   - **Spec:** does the summary match the todo item? If
     `BLOCKED:`, decide: re-spawn with more context, do it
     yourself, or `ask_user`.
   - **Code:** `host_shell("git diff --stat")` then read the
     diff. If a child's change is wrong,
     `host_shell("git checkout -- <file>")` and re-spawn.
   Only after BOTH pass: `todo(done, "<item>")`.

4. **Integrate** — `host_shell(<build/test cmd>)` on the
   combined result. Failures → identify which child's change
   broke it (bisect via `git stash` if needed), fix or
   re-spawn that one. Then `commit` → `ask_user("Open a
   PR?")` → `create_pr`.

For `spawn_parallel` results with `status:"conflict"`: the
child's fork is kept at `path`. Either resolve manually
(`host_shell("git -C <path> diff")` → `host_apply_patch`),
or discard (`host_shell("git worktree remove --force
<path>")`) and re-spawn that task sequentially.
