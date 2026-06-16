# PyInstaller spec — `cto` binary (Phase 10)
#
# Build:
#   make binary                          # native arch (host)
#   make binary-mac-arm64                # macOS arm64
#   make binary-mac-x86_64               # macOS x86_64 (build on Intel)
#
# Output: dist/cto-{platform}-{arch}    # single-file executable
#
# Scope: thin-client + wizard + doctor only. The chat REPL is
# included, but heavy ML deps (torch, sentence-transformers,
# fastembed) are EXCLUDED — they only matter for local-server
# mode, which the binary bootstraps via Docker rather than
# embedding.
#
# To run a full-fat binary that includes the in-process graph,
# unset CTO_BINARY_THIN=1 and rebuild — currently unsupported;
# adds ~600MB.

import os
import platform
import sys
from pathlib import Path

block_cipher = None

repo_root = Path(SPECPATH).parent.resolve()
src = repo_root / "src"

# Entry point
entry = str(src / "rag" / "api" / "cli.py")

# Hidden imports — PyInstaller's static analysis misses dynamic
# imports inside our subcommand routing and inside prompt_toolkit
# extensions. List them explicitly so the binary boots.
hiddenimports = [
    "rag.cli",
    "rag.cli._config",
    "rag.cli._health",
    "rag.cli.setup",
    "rag.cli.doctor",
    "rag.cli.compose",
    "rich",
    "rich.console",
    "rich.panel",
    "rich.rule",
    "rich.table",
    "rich.text",
    "rich.padding",
    "rich.markdown",
    "rich.live",
    "prompt_toolkit",
    "prompt_toolkit.completion",
    "prompt_toolkit.history",
    "prompt_toolkit.styles",
    "yaml",
    # Postgres URL parsing via psycopg is in scope ONLY when the
    # binary is invoked with local-mode + an existing postgres URL.
    # If absent, `cto doctor` falls back to a TCP probe.
]

# Excludes — keep the binary lean. The chat REPL is in scope
# (cli.py is the entry) so we have to ship some langchain bits,
# but we exclude the ML stack the in-process graph would need.
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "sentence_transformers",
    "fastembed",
    "qdrant_client",
    "psycopg",
    "psycopg2",
    "tree_sitter",
    "tree_sitter_java",
    "tree_sitter_python",
    "tree_sitter_javascript",
    "tree_sitter_go",
    "tree_sitter_typescript",
    "tree_sitter_cpp",
    "tree_sitter_rust",
    "tree_sitter_hcl",
    "openai",
    "anthropic",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "langgraph_checkpoint_postgres",
    "fastapi",
    "uvicorn",
    "gradio",
    "starlette",
    "sse_starlette",
    "pymupdf",
    "tavily",
    "phoenix",
    "openinference",
    "arize_phoenix_otel",
    "watchdog",
    "tiktoken",
    "litellm",
    # stdlib unused in the thin client
    "tkinter",
    "matplotlib",
    "PIL",
    "numpy",
    "scipy",
    "pandas",
    "IPython",
    "test",
    "unittest",
]

# Files to ship alongside the binary. The slim infra compose file
# is extracted to $XDG_DATA_HOME/cto/compose/docker-compose.yaml on
# first `cto compose` / wizard local-mode invocation.
datas = [
    (str(repo_root / "packaging" / "compose.cto-infra.yaml"), "."),
]
binaries: list = []

# When CTO_BINARY_THIN=0 is set, drop the excludes (full-fat
# binary; not officially supported yet).
if os.environ.get("CTO_BINARY_THIN", "1") != "1":
    excludes = []

a = Analysis(
    [entry],
    pathex=[str(src)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

arch = platform.machine().lower()  # arm64 | x86_64
osname = platform.system().lower()  # darwin | linux
suffix = f"{osname}-{arch}"
name = f"cto-{suffix}"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX shrinks but macOS Gatekeeper hates UPX'd binaries
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # native; cross-compile via separate runners
    codesign_identity=None,
    entitlements_file=None,
)
