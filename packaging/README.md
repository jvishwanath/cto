# Packaging — `cto` binary (Phase 10)

Thin-client PyInstaller build of the `cto` CLI.

## Scope

| In binary | Out of binary |
|---|---|
| `cto setup` wizard | LangGraph in-process graph |
| `cto doctor` diagnostics | torch / sentence-transformers / fastembed |
| `cto chat` (works against `--remote` server) | Qdrant client, Postgres driver |
| `cto chat` (works in-process via system Python install) | Anthropic / OpenAI SDKs |
| `cto skill list/verify` (Phase 10.1+) | Confluence / GitHub / JIRA Python clients |

The binary is **thin-client by default**: it talks to a CTO
server over HTTP/SSE. The server runs the ML stack. For
fully-local use, a developer install (`pip install -e .`) is
still the supported path.

Why thin: a fat binary including torch + transformers is ~700 MB
and slow to start. The thin binary is ~30-50 MB and starts in
<300ms.

## Build prerequisites

- Python 3.11+ (matches `pyproject.toml`)
- `pip install -e ".[binary]"` (adds PyInstaller)
- Native build only — PyInstaller does not cross-compile

## Build targets

```bash
make binary               # host architecture
make binary-mac-arm64     # macOS arm64 (must run on Apple Silicon)
make binary-mac-x86_64    # macOS x86_64 (must run on Intel Mac)
make binary-clean         # remove dist/ artifacts
```

Output: `dist/cto-{darwin}-{arm64|x86_64}`

## Distribution

Offline-only. Drop the artifact into your team's internal
artifact share (S3 bucket, internal Artifactory, shared NFS,
USB drive — whatever you use). No GitHub Releases, no
Homebrew tap, no auto-update channel.

Generate a checksum manually:

```bash
shasum -a 256 dist/cto-darwin-arm64 > dist/cto-darwin-arm64.sha256
```

Recipients verify with `shasum -c dist/cto-darwin-arm64.sha256`
before running.

## Verification

After building, smoke-test the binary in a clean shell:

```bash
./dist/cto-darwin-arm64 setup    # walk the wizard
./dist/cto-darwin-arm64 doctor   # connectivity report
./dist/cto-darwin-arm64 --help   # confirm chat REPL parser still works
```

## What ships inside

`packaging/pyinstaller_cto.spec` declares:

- **hiddenimports** — every `rag.cli.*` submodule, `rich.*`
  components, `prompt_toolkit.*` extensions, `yaml`. PyInstaller's
  static import analyzer misses dynamic imports in subcommand
  routing.
- **excludes** — the entire ML stack (torch, transformers,
  fastembed) plus framework deps that only the server needs
  (FastAPI, Gradio, langchain ecosystem, Qdrant/Postgres
  drivers). Excluding these shrinks the binary from ~700 MB to
  ~50 MB.

## Adding a new submodule

If you add a new module under `src/rag/cli/`, append it to the
`hiddenimports` list in the spec or PyInstaller will silently
omit it from the binary and the binary will crash at runtime
with `ModuleNotFoundError`.

## Known limits

- Linux + Windows builds not yet supported (out of scope for
  Phase 10 v1; macOS-only)
- Universal2 fat binaries (combined arm64+x86_64) not built —
  ship per-arch artifacts so users pick one
- macOS Gatekeeper will quarantine unsigned binaries.
  Workaround: `xattr -d com.apple.quarantine
  ./cto-darwin-arm64` on first run. Codesigning to be added in
  Phase 10.1.
