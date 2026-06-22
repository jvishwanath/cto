import os
from dotenv import load_dotenv

load_dotenv()

LITELLM_URL = os.environ.get("LITELLM_URL", "")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "postgresql://rag:rag@localhost:5432/rag")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
REPOS_DIR = os.environ.get("REPOS_DIR", str((__import__("pathlib").Path(__file__).parents[2] / "data" / "repos")))
WORKTREES_DIR = os.environ.get("WORKTREES_DIR", str((__import__("pathlib").Path(__file__).parents[2] / "data" / "superdev")))

VECTOR_DIM = int(os.environ.get("VECTOR_DIM", "1024"))

# Phase 8 — code mutation. Default OFF: the superdev graph isn't even
# importable in the read-only graph; enabling this is a deliberate
# operator decision per-host.
CTO_SUPERDEV_ENABLED = os.environ.get("CTO_SUPERDEV_ENABLED", "false").lower() == "true"
# create_pr (8-B): push + open MR/PR. Token never baked, never logged.
GIT_TOKEN = os.environ.get("GIT_TOKEN", "")
GITLAB_URL = os.environ.get("GITLAB_URL", "").rstrip("/")
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")

AGENT_MAX_ITER = int(os.environ.get("AGENT_MAX_ITER", "8"))

# Phoenix tracing (Phase 4.6). Set PHOENIX_ENABLED=false to disable.
PHOENIX_HOST = os.environ.get("PHOENIX_HOST", "http://localhost:6006")
PHOENIX_PROJECT = os.environ.get("PHOENIX_PROJECT", "cto")
PHOENIX_ENABLED = os.environ.get("PHOENIX_ENABLED", "true").lower() == "true"

# JIRA on-demand lookup (Phase 5). DC Personal Access Token (Bearer).
# Tools no-op gracefully when JIRA_TOKEN is unset.
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_USER = os.environ.get("JIRA_USER", "")  # Cloud Basic auth only
JIRA_VERIFY_SSL = os.environ.get("JIRA_VERIFY_SSL", "true").lower() == "true"

# MODELS = {
#     "embed": "text-embedding-3-large",
#     "router": "gpt-4.1-mini",
#     "agent": "claude-sonnet-4-6",
#     "agent_heavy": "claude-opus-4-7",
#     "fast": "gemini-3.1-flash-lite",
#     "vision": "claude-sonnet-4-6",  # vision-capable; reads image attachments
# }

# MODELS = {
#     "embed": "text-embedding-3-large",
#     "router": "gpt-5.1",
#     "agent": "claude-haiku-4-5",
#     "agent_heavy": "claude-haiku-4-5",
#     "fast": "gemini-3.1-flash-lite",
#     "vision": "claude-sonnet-4-6",  # vision-capable; reads image attachments
# }

MODELS = {
    "embed": "text-embedding-3-large",
    "router": "gpt-5",
    "agent": "gpt-5",
    "agent_heavy": "gpt-5",
    "fast": "gpt-5",
    "vision": "gpt-5",  # vision-capable; reads image attachments
}

# MODELS = {
#     "embed": "text-embedding-nomic-embed-text-v1.5",
#     "router": "qwen2.5-coder-7b-instruct-mlx",
#     "agent": "qwen2.5-coder-7b-instruct-mlx",
#     "agent_heavy": "qwen2.5-coder-7b-instruct-mlx",
#     "fast": "qwen2.5-coder-7b-instruct-mlx",
#     "vision": "qwen2.5-coder-7b-instruct-mlx",  # vision-capable; reads image attachments
# }

# Image attachments (Phase 6+). OCR fast-path → vision fallback.
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"
VISION_FALLBACK = os.environ.get("VISION_FALLBACK", "true").lower() == "true"
# Below this many usable chars, OCR is deemed insufficient → escalate
# to the vision model (likely a diagram, not a text screenshot).
OCR_MIN_CHARS = int(os.environ.get("OCR_MIN_CHARS", "24"))

# Guardrails (Phase 4.5). Set OFF_TOPIC_CHECK_ENABLED=false to bypass out-of-domain checks.
OFF_TOPIC_CHECK_ENABLED = os.environ.get("OFF_TOPIC_CHECK_ENABLED", "true").lower() == "true"
