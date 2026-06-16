**Systematic Debugging — find the root cause, not a symptom**

Do NOT patch the first plausible-looking line. Work the four
phases in order; `todo(set, items=[reproduce, isolate, fix,
verify])` and mark each `done` only when the exit criterion is
met.

1. **Reproduce** — get a deterministic, minimal repro.
   `host_shell` it. Exit: you can trigger the failure on
   demand with one command.
2. **Isolate** — bisect to the smallest change/input that
   flips pass↔fail. Use `git_log`/`git_show`/`git_blame` to
   find when it broke; `find_callers`/`search_code` to map
   data flow. State the root-cause hypothesis in one sentence.
   Exit: you can explain *why* it fails, not just *where*.
3. **Fix** — the smallest change that addresses the cause (not
   the symptom). If the fix is "add a null check", you're
   probably still at a symptom — go back to 2.
4. **Verify** — original repro now passes; the full suite
   passes; nothing else regressed. Add a regression test that
   would have caught this.

Never claim "fixed" without step 4 actually run via
`host_shell`.
