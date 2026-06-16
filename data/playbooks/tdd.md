**Test-Driven Development — RED · GREEN · REFACTOR**

You will NOT write production code before a failing test
exists for it. Treat this as a hard rule.

1. **RED** — write the smallest test that captures the next
   behaviour. Run it (`host_shell`). Confirm it FAILS for the
   right reason. If it passes, the test is wrong.
2. **GREEN** — write the minimum code to make that one test
   pass. No extra abstraction, no "while I'm here." Run the
   full suite — everything green.
3. **REFACTOR** — only now improve structure, with the suite
   as your safety net. Run tests after every change.

Per cycle: `todo(done, "<step>")`. If you catch yourself
writing implementation before a failing test, stop, delete it,
and go back to RED.

If the test command exits non-zero for *infrastructure*
reasons (`./gradlew: not found`, exit 127, missing wrapper),
that is NOT a passing test — `ask_user` whether to install
the tooling, use `docker_run` with a build image, or proceed
without verification (and say so in the commit message). Do
NOT mark RED/GREEN steps done on a 127.

If the codebase has no test runner, FIRST `ask_user` which
framework to set up — do not guess.
