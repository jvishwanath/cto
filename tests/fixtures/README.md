# Test fixtures

`acme-auth` and `acme-api` are tiny synthetic Spring/Java repos
used by the smoke tests, eval golden set, and benchmarks. They
contain just enough surface (`validateCSR`, `CryptoServiceImpl`,
`ClientOnboardingServiceImpl`, a controller, `application
.properties`) for retrieval, code-graph, grep, read_file, and
sandbox-mount checks to assert against.

## Setup

The fixtures are tracked **without** a `.git` dir (it would
collide with this repo's). `make fixtures` initializes them as
real git repos and symlinks them into `data/repos/` so the
indexer/watcher/git tools see them:

```bash
make fixtures      # git init each, one commit, ln -s into data/repos/
make index-all     # index them into Qdrant + the code graph
make test          # smoke tests now pass against acme-*
```

Idempotent — re-running `make fixtures` is a no-op once set up.
You can keep your own real repos under `data/repos/` alongside
these; the tests only assert on `acme-auth`/`acme-api`.
