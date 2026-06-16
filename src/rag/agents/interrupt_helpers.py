"""
Client-side helpers for handling LangGraph interrupts emitted by the
clarify node. Lets ask.py / ui.py / app.py stay agnostic of langgraph
internals.
"""

from typing import Any

from langgraph.types import Command, Interrupt

INTERRUPT_KEY = "__interrupt__"


def extract_interrupt(stream_event: Any) -> dict | None:
    """
    Given one event yielded by graph.stream(), return the interrupt
    payload dict (the value passed to interrupt()) if this event IS an
    interrupt, else None.

    Handles:
      - stream_mode="updates" / "values":
            {'__interrupt__': (Interrupt(value=..., id=...),)}
      - stream_mode=["messages","updates"]:
            ('updates', {'__interrupt__': (...)})
      - already-unwrapped Interrupt instances or tuples thereof.
    """
    payload = stream_event

    if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], str):
        payload = payload[1]

    if isinstance(payload, dict):
        if INTERRUPT_KEY not in payload:
            return None
        payload = payload[INTERRUPT_KEY]

    if isinstance(payload, (tuple, list)):
        if not payload:
            return None
        payload = payload[0]

    if isinstance(payload, Interrupt):
        return payload.value
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    return None


def resume_command(answer: Any) -> Command:
    """Wrap a user answer for graph.stream(Command(resume=answer), ...)."""
    return Command(resume=answer)
