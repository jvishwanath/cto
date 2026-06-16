"""
Phoenix tracing (Phase 4.6) — OpenTelemetry / OpenInference.

Call `init_tracing()` ONCE at process start (FastAPI lifespan, top of
ask.py). After that, every LangChain/LangGraph run is auto-instrumented
— no per-request callback needed.

`trace_config()` is a thin helper that builds the per-request
RunnableConfig and returns a `Tracer` handle for attaching session/user
metadata, fetching the trace URL, and pushing eval scores as span
feedback.

Everything degrades to no-op when PHOENIX_ENABLED is false or the
packages aren't installed.
"""

from __future__ import annotations
import contextlib
import logging
from typing import Any

from .config import PHOENIX_ENABLED, PHOENIX_HOST, PHOENIX_PROJECT

log = logging.getLogger(__name__)

_PHASE_TAG = "phase-4.6"
_initialized = False
_tracer_provider: Any | None = None
_unavailable: str | None = None
_project_id: str | None = None


def _resolve_project_id() -> str | None:
    """Phoenix routes /projects/<id> by opaque GraphQL node ID, not
    name. Look it up once via the REST API; cache for the process."""
    global _project_id
    if _project_id is not None:
        return _project_id
    try:
        import urllib.request, json
        with urllib.request.urlopen(
            f"{PHOENIX_HOST.rstrip('/')}/v1/projects", timeout=3
        ) as r:
            for p in json.load(r).get("data", []):
                if p.get("name") == PHOENIX_PROJECT:
                    _project_id = p.get("id")
                    return _project_id
    except Exception as e:
        log.debug("phoenix project-id lookup failed: %s", e)
    return None


def init_tracing(*, quiet: bool = False) -> bool:
    """Idempotent. Registers Phoenix OTLP exporter + LangChain
    auto-instrumentation. Returns True if tracing is active."""
    global _initialized, _tracer_provider, _unavailable
    if _initialized:
        return _tracer_provider is not None
    _initialized = True

    if not PHOENIX_ENABLED:
        return False

    try:
        from phoenix.otel import register
        from openinference.instrumentation.langchain import (
            LangChainInstrumentor,
        )
    except ImportError as e:
        _unavailable = str(e)
        log.warning(
            "phoenix tracing unavailable (%s); run: "
            "pip install arize-phoenix-otel "
            "openinference-instrumentation-langchain", e,
        )
        return False

    try:
        import contextlib, io
        sink = contextlib.redirect_stdout(io.StringIO()) if quiet \
            else contextlib.nullcontext()
        with sink:
            _tracer_provider = register(
                project_name=PHOENIX_PROJECT,
                endpoint=f"{PHOENIX_HOST.rstrip('/')}/v1/traces",
                batch=True,
            )
        LangChainInstrumentor().instrument(tracer_provider=_tracer_provider)
        log.info("Phoenix tracing → %s (project=%s)",
                 PHOENIX_HOST, PHOENIX_PROJECT)
    except Exception as e:
        _unavailable = f"{type(e).__name__}: {e}"
        log.warning("phoenix register failed (%s); tracing disabled", e)
        _tracer_provider = None
    return _tracer_provider is not None


def enabled() -> bool:
    return _tracer_provider is not None


# ── Per-request tracer handle ────────────────────────────────────────

class _Tracer:
    """Wraps an OpenInference `using_session` context so the LangGraph
    run is tagged with session.id / user.id / tags, and exposes the
    root trace URL after the run completes.

    With suppress=True, enters `suppress_tracing()` instead — the
    LangChainInstrumentor checks that contextvar before every span
    creation, so the request runs with zero tracing overhead even
    though instrumentation is globally registered."""

    def __init__(self, thread_id: str, user_id: str | None,
                 tags: list[str], *, suppress: bool = False) -> None:
        self._thread_id = thread_id
        self._user_id = user_id
        self._tags = tags
        self._suppress = suppress
        self._stack = contextlib.ExitStack()
        self._trace_id: str | None = None

    @property
    def is_enabled(self) -> bool:
        return enabled() and not self._suppress

    def __enter__(self) -> "_Tracer":
        if not enabled():
            return self
        try:
            if self._suppress:
                from openinference.instrumentation import suppress_tracing
                self._stack.enter_context(suppress_tracing())
                return self
            from openinference.instrumentation import (
                using_session, using_user, using_metadata,
            )
            self._stack.enter_context(using_session(self._thread_id))
            if self._user_id:
                self._stack.enter_context(using_user(self._user_id))
            self._stack.enter_context(using_metadata({
                "tags": self._tags, "phase": _PHASE_TAG,
            }))
        except Exception as e:
            log.debug("phoenix context attach failed: %s", e)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()
        self.flush()

    def capture_trace_id(self) -> None:
        """Call inside the context AFTER the graph has started so the
        root span exists. Best-effort."""
        if not self.is_enabled or self._trace_id:
            return
        try:
            from opentelemetry import trace
            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                self._trace_id = format(ctx.trace_id, "032x")
        except Exception:
            pass

    def url(self) -> str | None:
        if not self.is_enabled:
            return None
        pid = _resolve_project_id()
        host = PHOENIX_HOST.rstrip("/")
        if not pid:
            return f"{host}/projects"
        if self._trace_id:
            return f"{host}/projects/{pid}/traces/{self._trace_id}"
        return f"{host}/projects/{pid}/sessions"

    def score(self, name: str, value: float,
              comment: str | None = None) -> None:
        """Attach as a span event on the current/root span."""
        if not self.is_enabled:
            return
        try:
            from opentelemetry import trace
            span = trace.get_current_span()
            span.add_event("score", {
                "score.name": name,
                "score.value": float(value),
                "score.comment": comment or "",
            })
        except Exception as e:
            log.debug("phoenix score failed: %s", e)

    def flush(self) -> None:
        if _tracer_provider is not None:
            try:
                _tracer_provider.force_flush(timeout_millis=5000)
            except Exception:
                pass


def trace_config(
    thread_id: str,
    *,
    user_id: str | None = None,
    query: str | None = None,
    tags: list[str] | None = None,
    suppress: bool = False,
) -> tuple[dict, _Tracer]:
    """Returns (config_dict, tracer). Use as:

        cfg, tracer = trace_config(sid, user_id=u, query=q)
        with tracer:
            for ev in app.stream(inputs, config=cfg): ...
        url = tracer.url()
    """
    init_tracing()
    cfg: dict = {
        "configurable": {"thread_id": thread_id},
        "run_name": (query[:80] if query else "query"),
        "tags": [_PHASE_TAG, *(tags or [])],
        "metadata": {
            "session.id": thread_id,
            "user.id": user_id,
            "query": query,
        },
    }
    return cfg, _Tracer(thread_id, user_id, [_PHASE_TAG, *(tags or [])],
                        suppress=suppress)


def push_scores(tracer: _Tracer, final_state: dict) -> None:
    """Attach evaluator + guard outputs as score events."""
    if not tracer.is_enabled:
        return
    ev = (final_state.get("eval_scores") or {})
    ans = ev.get("answer") or {}
    ret = ev.get("retrieval") or {}
    flags = final_state.get("guard_flags") or {}

    for name, val in (
        ("groundedness", ans.get("groundedness")),
        ("completeness", ans.get("completeness")),
        ("eval_combined", ans.get("combined")),
    ):
        if val is not None:
            tracer.score(name, float(val), comment=ans.get("critique") or None)

    if ret.get("n_chunks"):
        tracer.score("retrieval_relevance",
                     ret.get("n_relevant", 0) / ret["n_chunks"],
                     comment=f"{ret.get('n_relevant')}/{ret['n_chunks']} relevant")

    if flags.get("blocked"):
        tracer.score("blocked", 1.0, comment=flags["blocked"])
    if flags.get("abstained"):
        tracer.score("abstained", 1.0,
                     comment=f"max_score={flags.get('max_retrieval_score')}")
    if (n := final_state.get("eval_iter")):
        tracer.score("eval_retries", float(n))
