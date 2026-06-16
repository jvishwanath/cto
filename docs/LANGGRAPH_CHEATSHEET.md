# LangChain / LangGraph Cheatsheet

> APIs used in this project, organized by module. Each entry: what it does + where it's used.

## LangGraph — Graph Construction (`langgraph.graph`)

| API | Where used | What it does |
|---|---|---|
| `StateGraph(StateType)` | `graph.py` | Create a graph builder typed to your state schema |
| `.add_node(name, fn)` | `graph.py` | Register a node. `fn(state) -> dict` returns state deltas |
| `.add_edge(from, to)` | `graph.py` | Fixed transition: always go `from → to` |
| `.add_conditional_edges(from, route_fn, mapping)` | `graph.py` | Branching: `route_fn(state) -> str` picks next node via `mapping[str]` |
| `.set_entry_point(name)` / `.add_edge(START, name)` | `graph.py` | Where execution begins |
| `START`, `END` | `graph.py` | Sentinel nodes for graph entry/exit |
| `.compile(checkpointer=...)` | `graph.py` | Build runnable graph; checkpointer enables persistence |

## LangGraph — Execution

| API | Where used | What it does |
|---|---|---|
| `app.invoke(inputs, config)` | `api/app.py`, `ask.py` | Run graph to completion, return final state |
| `app.stream(inputs, config, stream_mode)` | `api/app.py`, `ask.py` | Generator yielding events as graph runs |
| `stream_mode="updates"` | `api/app.py` | Yield `{node_name: delta}` after each node |
| `stream_mode="values"` | (not yet) | Yield full state after each node |
| `stream_mode="messages"` | (not yet) | Yield LLM tokens as generated (word-by-word) |
| `config={"configurable": {"thread_id": X}}` | `api/app.py`, `ask.py` | Session key — checkpointer loads/saves state per thread |
| `config={"recursion_limit": N}` | `agent_loop.py` | Cap total node executions (prevents infinite loops in cyclic graphs) |

## LangGraph — State & Reducers (`langgraph.graph.message`, `typing`)

| API | Where used | What it does |
|---|---|---|
| `TypedDict` | `state.py` | Define state schema with typed keys |
| `Annotated[T, reducer]` | `state.py` | Attach a merge function to a state key |
| `add_messages` | `state.py` | Reducer: append messages, dedup by `id`, handle updates |
| `operator.add` | `state.py` | Reducer: list concatenation (parallel branches merge) |
| *(no annotation)* | `state.py` | Default reducer: last write wins (overwrite) |

## LangGraph — Persistence (`langgraph.checkpoint.postgres`)

| API | Where used | What it does |
|---|---|---|
| `PostgresSaver(conn)` | `checkpointer.py` | Checkpointer backed by Postgres |
| `.setup()` | `checkpointer.py` | Create `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` tables |
| `MemorySaver()` | (not used) | In-memory checkpointer (dev/tests, no persistence) |

## LangGraph — Prebuilt (`langgraph.prebuilt`)

| API | Where used | What it does |
|---|---|---|
| `create_react_agent(model, tools, state_modifier)` | `agent_loop.py` | One-liner ReAct agent: builds a 2-node `agent ↔ tools` cyclic subgraph |
| `state_modifier=` | `agent_loop.py` | System prompt (string) or `fn(state) -> messages` |

## LangChain — LLM Client (`langchain_openai`)

| API | Where used | What it does |
|---|---|---|
| `ChatOpenAI(base_url, api_key, model, ...)` | `llm.py` | OpenAI-compatible chat client (we point it at LiteLLM gateway) |
| `.invoke(messages)` | all nodes | Single call → returns `AIMessage` |
| `.bind_tools(tools)` | `agent_loop.py` | Returns new client that sends tool schemas with every request |
| `temperature=0` | `llm.py` | Deterministic output (no sampling randomness) |
| `streaming=True` | (not yet) | Token-by-token streaming |

## LangChain — Messages (`langchain_core.messages`)

| Class | Where used | What it represents |
|---|---|---|
| `BaseMessage` | `state.py` | Abstract base — type hint for `messages: list[BaseMessage]` |
| `SystemMessage(content)` | nodes | Instructions to the model (not shown to user) |
| `HumanMessage(content)` | `api/app.py`, `ask.py` | User input |
| `AIMessage(content, tool_calls)` | `agent_loop.py` | Model output. `.tool_calls` = list of `{name, args, id}` if model requested tools |
| `ToolMessage(content, tool_call_id)` | `agent_loop.py` | Result of executing a tool; `tool_call_id` links it to the `AIMessage` that requested it |
| `msg.type` | `router.py` | String: `"human"` / `"ai"` / `"tool"` / `"system"` |

## LangChain — Tools (`langchain_core.tools`)

| API | Where used | What it does |
|---|---|---|
| `@tool` decorator | `tools/*.py` | Converts a Python function into a tool. Auto-generates JSON schema from type hints + docstring |
| `tool.name` | `tools/__init__.py` | Tool identifier (function name by default) |
| `tool.invoke(args_dict)` | `agent_loop.py` | Execute the tool with parsed arguments |
| `tool.args_schema` | (implicit) | The Pydantic model derived from the function signature |

## Patterns / Idioms

| Pattern | Code | Why |
|---|---|---|
| **Return deltas, not full state** | `return {"route": x}` not `return {**state, "route": x}` | Reducers handle merging; full state breaks `operator.add` |
| **Lazy singleton** | `_model = None; def _get(): global _model; if _model is None: ...` | Don't connect/load at import time |
| **System prompt outside checkpointed messages** | Prepend `SystemMessage` in node, don't store in `state["messages"]` | Static prompt shouldn't bloat persisted history |
| **Wrap tool output** | `ToolMessage(content=f"<retrieved>{result}</retrieved>")` | Prompt-injection guard — model treats it as data |
| **Conditional edge function** | `def route_decision(state) -> str: return state["route"]` | Separates "compute route" (node) from "read route" (edge) |

## Not Yet Used (Coming in Later Phases)

| API | Phase | Purpose |
|---|---|---|
| `Send(node, state)` | 4 | Fan-out: dispatch N parallel copies of a node with different inputs |
| `interrupt(value)` | 4 | Pause graph, return value to caller, resume on next invoke (human-in-the-loop) |
| `Command(goto=, update=)` | 4 | Node returns both state update AND next-node decision in one |
| `BaseStore` / `PostgresStore` | 4 | Long-term cross-session memory (vs checkpointer = per-thread) |
| `graph.get_state(config)` | 4 | Inspect current checkpointed state without running |
| `graph.update_state(config, values)` | 4 | Manually patch state (e.g., inject human correction) |
| Subgraph as node | 3 | `g.add_node("exec", compiled_subgraph)` — nest graphs |
