"""
Verbosity control — shared by agent_loop / simple_rag / deep_research.

Three modes:
  terse    — direct answer only, ≤3 sentences, no background/recommendations
  normal   — concise answer first; OFFER to expand if more exists (option D)
  detailed — comprehensive: evidence, context, alternatives, caveats

Set via (in priority order):
  1. Inline prefix on the query: "q: …", "brief: …", "detail: …" (option B)
  2. state["verbosity"] from --terse/--detailed, ?verbosity=, UI toggle (option A)
  3. Default → "normal"
"""

import re

LEVELS = ("terse", "normal", "detailed")

_PREFIX_RE = re.compile(
    r"^\s*(?P<pfx>q|brief|terse|short|tl;?dr|quick"
    r"|detail(?:ed)?|full|deep|explain|long)\s*[:\-—]\s*",
    re.IGNORECASE,
)

_PREFIX_MAP = {
    "q": "terse", "brief": "terse", "terse": "terse", "short": "terse",
    "tldr": "terse", "tl;dr": "terse", "quick": "terse",
    "detail": "detailed", "detailed": "detailed", "full": "detailed",
    "deep": "detailed", "explain": "detailed", "long": "detailed",
}

_INSTRUCTIONS = {
    "terse": (
        "RESPONSE LENGTH: TERSE. Answer in ≤3 sentences. Direct answer "
        "only — no background, no 'what this repo actually does', no "
        "recommendations, no headers/tables unless the answer IS a list. "
        "Still cite [SOURCE_N]."
    ),
    "normal": (
        "RESPONSE LENGTH: CONCISE BY DEFAULT. Lead with the direct answer "
        "in 2-4 sentences. Add at most ONE short supporting section if it "
        "materially helps. If you have significantly more useful detail "
        "(comparison tables, full call chains, recommendations), do NOT "
        "include it — instead end with exactly one line: "
        "'_(Reply \"more\" for the full breakdown.)_' "
        "If there isn't meaningfully more, omit that line."
    ),
    "detailed": (
        "RESPONSE LENGTH: COMPREHENSIVE. Provide the full breakdown — "
        "evidence, structured comparison (tables where helpful), call "
        "chains, caveats, and recommendations. Use headers to organize."
    ),
}

# Replies that mean "expand the previous answer" (option D follow-up).
# Anchored whole-string so "more details on JWT" (a real follow-up
# question) is NOT caught — only bare expand requests.
_EXPAND_RE = re.compile(
    r"^\s*"
    r"(?:(?:please|pls|can you|could you)\s+)?"
    r"(?:(?:provide|give(?:\s+me)?|show(?:\s+me)?|explain|add|share)\s+)?"
    r"(?:(?:some|a\s+bit|a\s+little)\s+)?"
    r"(?:more(?:\s+(?:details?|info(?:rmation)?|context|depth))?"
    r"|expand(?:\s+(?:on\s+(?:that|this|it)|that|this|it))?"
    r"|full(?:\s+(?:answer|details?))?"
    r"|details?|elaborate(?:\s+(?:on\s+(?:that|this|it)))?"
    r"|go\s+(?:on|deeper)|continue|keep\s+going|deeper"
    r"|tell\s+me\s+more"
    r"|yes(?:[,\s]+(?:more|please|expand|details?))*"
    r")"
    r"(?:\s+(?:please|pls))?"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def parse_prefix(query: str) -> tuple[str, str | None]:
    """If query starts with a verbosity prefix, strip it and return
    (stripped_query, level). Else (query, None)."""
    m = _PREFIX_RE.match(query)
    if not m:
        return query, None
    level = _PREFIX_MAP.get(m.group("pfx").lower().replace(";", ""))
    return query[m.end():], level


def is_expand_request(query: str) -> bool:
    """True if the query is a bare 'more'/'expand'/'details' follow-up."""
    return bool(_EXPAND_RE.match(query))


def instruction(level: str | None) -> str:
    """Prompt suffix for the given verbosity level."""
    return _INSTRUCTIONS.get(level or "normal", _INSTRUCTIONS["normal"])


def resolve(state: dict) -> str:
    """Effective verbosity for this turn."""
    v = state.get("verbosity")
    return v if v in LEVELS else "normal"
