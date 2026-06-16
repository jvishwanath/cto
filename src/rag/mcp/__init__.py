"""
MCP (Model Context Protocol) client integration.

Consumes 3rd-party MCP servers (filesystem, github, sqlite, sentry,
sequential-thinking, memory, fetch, …) and exposes their tools,
resources, and prompts inside the /superdev graph. The read-only Q&A
graph never imports this package.

See docs/MCP.md for scope, deferred items, and config schema.
"""
