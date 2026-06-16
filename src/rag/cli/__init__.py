"""Phase 10 — distribution layer for the binary CLI.

Holds subcommands invokable as `cto setup`, `cto doctor`, etc.
The default invocation `cto [question]` still runs the chat REPL
defined in `rag.api.cli:main` — this package adds setup/doctor
without rewriting the chat code path.

Layout:
    _config.py — XDG-compliant config + credentials read/write
    _health.py — connection probes (LLM gateway, Qdrant, etc.)
    setup.py   — interactive wizard (remote + local-Docker modes)
    doctor.py  — diagnostic checks for support tickets
"""
