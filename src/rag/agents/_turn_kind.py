"""Classify a superdev turn as 'read' (cacheable, gradable) vs
'action' (mutating, must skip cache + evaluator).

Used by the wrapper pipeline in superdev_graph: load_memories runs
unconditionally, but evaluator/output_guard's abstain logic and the
CLI's cache_store gate only make sense for read-only turns.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage

# Tools that DO NOT mutate host / repo / external systems. Anything
# else (host_write, host_edit, host_shell, commit, create_pr, docker_run,
# spawn*, jira writes, MCP mutating tools, …) → action turn.
READ_TOOLS = frozenset({
    "search", "search_code", "search_docs",
    "find_symbol", "find_callers", "find_callees",
    "grep", "read_file", "repo_info",
    "git_log", "git_show", "git_blame", "find_commits_for_jira",
    "web_search", "web_fetch",
    "host_read", "host_glob",
    "jira_lookup", "jira_search", "jira_list_transitions",
    "todo", "ask_user",
})

# Read-only MCP tool basenames common across servers we ship in
# .mcp.json.example. MCP tools are exposed as `<server>__<tool>`;
# we strip the prefix before lookup.
READ_MCP_BASES = frozenset({
    "echo", "list_directory", "list_directory_with_sizes",
    "directory_tree", "read_file", "read_text_file",
    "read_media_file", "read_multiple_files", "get_file_info",
    "search_files", "list_allowed_directories",
    "fetch", "search_nodes", "open_nodes", "read_graph",
    "list_resources", "read_resource",
    "list_prompts", "use_prompt",
    "sequentialthinking",
})


def classify_turn(messages_delta: list[BaseMessage]) -> str:
    """'action' if any AIMessage in the delta called a non-read tool;
    'read' otherwise (covers pure-LLM turns and read-only tool use)."""
    for m in messages_delta:
        if not isinstance(m, AIMessage):
            continue
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name") or ""
            if "__" in name:                       # MCP <server>__<tool>
                base = name.split("__", 1)[1]
                if base in READ_MCP_BASES:
                    continue
                return "action"
            if name not in READ_TOOLS:
                return "action"
    return "read"
