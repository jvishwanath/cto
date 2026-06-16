**Verification Before Completion**

"It compiles" ≠ done. "Tests pass" ≠ done. Before you say a
task is complete you MUST have:

- Run the actual behaviour (`host_shell` the binary/endpoint/
  CLI) and observed the expected output, not just inferred it.
- Run the full test suite, not just the file you touched.
- Checked `find_callers` for every signature you changed —
  list each caller and confirm it still works (or is updated).
- `host_shell("git -C <wt> diff")` and read your own diff end
  to end. If anything surprises you, it'll surprise the
  reviewer.

If any of these is impossible in this environment, say so
explicitly ("could not run X because Y") — do not silently
skip and claim done.

Regardless of verification outcome, if the worktree is
dirty you MUST `commit` and `ask_user("Open a PR?")` before
your final answer. "Couldn't verify" is a note in the PR
body, not a reason to leave changes uncommitted.
