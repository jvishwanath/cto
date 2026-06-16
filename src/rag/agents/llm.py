"""
LLM factory — all models go through the corp LiteLLM gateway via the
OpenAI-compatible client.
"""

from langchain_openai import ChatOpenAI

from ..config import LITELLM_URL, LITELLM_API_KEY, MODELS


_DEFAULT_TIMEOUTS = {
    "router": 30.0,    # query_analysis / classifier — short, structured
    "fast": 30.0,
    "agent": 120.0,    # may stream long answers + tool calls
    "agent_heavy": 180.0,
    "vision": 60.0,
    "embed": 30.0,
}


def llm(role: str, **kwargs) -> ChatOpenAI:
    """Get a chat model by logical role: 'router', 'agent', 'agent_heavy', 'fast'."""
    # Stream by default for agent roles so LangGraph stream_mode='messages'
    # can yield tokens mid-node. Router stays non-streaming (it's one word).
    kwargs.setdefault("streaming", role != "router")
    # Hard timeout — without one, a stuck gateway connection hangs the
    # whole graph indefinitely (observed: save_memories blocking 65s).
    kwargs.setdefault("request_timeout",
                      _DEFAULT_TIMEOUTS.get(role, 90.0))
    kwargs.setdefault("max_retries", 1)
    return ChatOpenAI(
        base_url=LITELLM_URL,
        api_key=LITELLM_API_KEY,
        model=MODELS[role],
        **kwargs,
    )
