# Deploying CTO on EC2

One docker-compose stack runs four containers: **Qdrant** (vectors),
**Postgres** (checkpointer + code graph + Confluence sync state),
**Phoenix** (tracing UI), **app** (FastAPI server + Gradio UI + watcher
+ Confluence scheduler). All persistent state lives under `./data/`
and `./hf-cache/` on the host — bind-mounted, so the containers are
disposable.

```
┌──────────────── EC2 host ───────────────────────────────────────────┐
│  ./data/                ./hf-cache/                                  │
│   ├─ repos/   (clones)   └─ …/BAAI/bge-reranker-v2-m3   ←─┐          │
│   ├─ docs/                                                │ mounted  │
│   ├─ connectors/confluence.yaml                           │          │
│   ├─ qdrant/   ─┐                                         │          │
│   ├─ postgres/  ├─ container volumes                      │          │
│   └─ phoenix/  ─┘                                         │          │
│                                                           │          │
│   docker compose:  qdrant  postgres  phoenix   app ───────┘          │
│                                                 │ :8000              │
└─────────────────────────────────────────────────┼────────────────────┘
                         browser → /ui            │
                         laptop  → cto --remote ──┘  (thin SSE client)
```

---

## 1. EC2 prerequisites

| | |
|---|---|
| Instance | `t3.xlarge`+ (4 vCPU, 16 GB) — reranker is CPU-only here |
| Disk | 50 GB+ gp3 (Qdrant + Postgres + HF cache ≈ 5 GB; rest is repos) |
| AMI | Amazon Linux 2023 or Ubuntu 22.04 |
| SG inbound | `8000` (UI + API), `6006` (Phoenix UI). Keep `5432`/`6333` **closed** — only the app container needs them, over the compose network |
| Outbound | LiteLLM gateway, Confluence, JIRA, GitLab |

```bash
# Amazon Linux 2023
sudo dnf install -y docker git make
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
sudo curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
     -o /usr/local/bin/docker-compose && sudo chmod +x /usr/local/bin/docker-compose
```

---

## 2. Clone + configure

```bash
git clone <this-repo> cto && cd cto
cp .env.example .env
$EDITOR .env                      # set LITELLM_API_KEY, CONFLUENCE_TOKEN, JIRA_TOKEN
                                  # (optionally) CA_CERT=ops/corp-root-ca.crt
cp data/connectors/confluence.yaml.example data/connectors/confluence.yaml
$EDITOR data/connectors/confluence.yaml
```

> The compose `environment:` block already overrides `QDRANT_URL`,
> `POSTGRES_DSN`, `PHOENIX_HOST` to the in-network service hostnames —
> leave the `localhost` defaults in `.env` (they're for laptop dev).

---

## 3. Mount the data directory

`./data` and `./hf-cache` are plain host directories; compose
bind-mounts them into the containers. To put them on a separate EBS
volume:

```bash
sudo mkfs.xfs /dev/nvme1n1
sudo mkdir -p /mnt/cto && sudo mount /dev/nvme1n1 /mnt/cto
echo '/dev/nvme1n1 /mnt/cto xfs defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo chown -R $USER /mnt/cto

ln -s /mnt/cto/data     ./data
ln -s /mnt/cto/hf-cache ./hf-cache
mkdir -p data/{repos,docs,connectors,qdrant,postgres,phoenix} hf-cache
```

Repos must be **real clones** on the EC2 disk (the laptop's symlinks
won't resolve here, and `git_history` tools need `.git/`):

```bash
git clone git@gitlab:org/acme-auth data/repos/acme-auth
git clone git@gitlab:org/acme-api    data/repos/acme-api
# …etc
```

---

## 4. Build the image + pre-warm the reranker (one-time)

The `bge-reranker-v2-m3` model (~1.1 GB) is **not** baked into the
image. It's downloaded once into `./hf-cache/` and bind-mounted at
`/hf-cache`; the app loads it with `local_files_only=True` so startup
never hits HuggingFace.

```bash
make docker-build          # builds cto-app:latest
make prewarm-reranker      # populates ./hf-cache via a one-shot container
ls hf-cache/hub/models--BAAI--bge-reranker-v2-m3   # sanity
```

If the EC2 host can't reach `huggingface.co` (corp proxy), download on
your laptop and `rsync -av hf-cache/ ec2:/mnt/cto/hf-cache/` instead.

---

## 5. Bring up the stack

```bash
make docker-up
make docker-logs           # wait for "Uvicorn running on http://0.0.0.0:8000"
                           #           "reranker warmed"
curl -s localhost:8000/health      # → {"status":"ok"}
```

| Endpoint | URL |
|---|---|
| Web UI | `http://<ec2>:8000/ui` |
| API | `http://<ec2>:8000/query` (SSE), `/sources`, `/sessions` |
| Phoenix traces | `http://<ec2>:6006` |

---

## 6. Index the data (inside the container)

All `make index-*` / `confluence-*` targets run **inside** the app
container so they see the same `data/` mount and talk to
`qdrant`/`postgres` by service name:

```bash
make docker-exec T=index-all                 # full Qdrant rebuild
make docker-exec T=index-graph               # Postgres code graph
make docker-exec T=confluence-sync           # initial Confluence pull
make docker-exec T="index-add REPO=acme-foo" # add one repo later
```

The watcher inside the container picks up file changes under
`data/repos/` and `data/docs/` automatically thereafter.

---

## 7. CLI from your laptop — thin remote client

The CLI has two modes. With `--remote` (or `CTO_REMOTE` env) it's a
**pure HTTP/SSE client** — no torch, no langgraph, no qdrant-client
required locally. Same `⏺`/`⎿` rendering, same clarify prompts, same
slash commands (routed to `GET /sources`, `GET /sessions`).

```bash
# laptop — minimal install (3 packages, no ML stack)
git clone <this-repo> cto && cd cto
python3 -m venv .venv && source .venv/bin/activate
pip install --no-deps -e . && pip install httpx rich prompt_toolkit

export CTO_REMOTE=http://<ec2>:8000
cto                                # interactive REPL
cto "where is validateCSR defined"  # one-shot
make chat-remote R=http://<ec2>:8000   # equivalent
```

What's identical to local mode: trace bullets, clarify panel, citations,
`/sources`, `/sessions`, `/new`, cache-hit badge, trace URL.
What's different: the **answer arrives whole**, not token-streamed
(server uses `stream_mode="updates"` — tool activity still streams
live, only the final text appears at once). Caching happens
server-side.

---

## 8. Operations cheatsheet

| Task | Command |
|---|---|
| Tail app logs | `make docker-logs` |
| Shell into app | `make docker-shell` |
| Rebuild + restart app only | `make docker-restart` |
| Restart everything | `make docker-down && make docker-up` |
| Add a repo | `git clone … data/repos/<name>` then `make docker-exec T="index-add REPO=<name>"` |
| Re-pull repos nightly | cron: `for r in data/repos/*; do git -C "$r" pull --ff-only; done` (watcher re-indexes) |
| Purge cache | `make docker-exec T=cache-purge` |
| Wipe one session | `make docker-exec T="purge-sessions S=<id>"` |
| Backup | stop stack → snapshot the EBS volume (or `tar czf data.tgz data hf-cache`) |

---

## 9. Securing it

Three modes; pick per environment. All are **off by default** (open)
so laptop dev is unchanged.

### 9a. API key (quickest — protects CLI + raw API)

```bash
# on EC2, in .env:
CTO_API_KEYS=$(make gen-api-key),$(make gen-api-key)   # one per consumer
make docker-restart
```

`/query`, `/sources`, `/sessions` now return 401 without
`Authorization: Bearer <key>`. `/health` stays open. On the laptop:

```bash
export CTO_API_KEY=<one-of-the-keys>
cto --remote http://<ec2>:8000              # header sent automatically
```

### 9b. UI login form

```bash
CTO_UI_USER=admin
CTO_UI_PASSWORD=$(make gen-api-key)
```

Gradio renders a login page at `/ui`. Skip this if you front with
9c — the proxy already gates the browser.

### 9c. OIDC via oauth2-proxy (real users, real audit)

Fronts the whole app with Okta/Azure AD/Google. Browser users get
SSO; the proxy injects `X-Forwarded-User`, which the app trusts as
`user_id` (so Phoenix traces and `Store` memories are per-person, not
per-session).

```bash
# .env on EC2:
OIDC_ISSUER_URL=https://<tenant>.okta.com/oauth2/default
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URL=http://<ec2>:8080/oauth2/callback
OIDC_EMAIL_DOMAINS=example.com
OAUTH2_COOKIE_SECRET=$(make gen-api-key)
CTO_TRUST_FORWARDED_USER=true
# keep CTO_API_KEYS too — CLI bypasses OIDC with Bearer

# remove the `ports: ["8000:8000"]` from app (proxy-only ingress), then:
docker compose --profile oidc up -d
```

Browse `http://<ec2>:8080/ui` → IdP login → app. The CLI keeps
hitting `:8000` directly with its Bearer key (or, if `:8000` is
closed, point it at `:8080` and let oauth2-proxy validate the key via
`--skip-jwt-bearer-tokens` — provider-specific).

> On AWS, **ALB + OIDC listener rule** is equivalent and avoids the
> extra container — same `X-Forwarded-User` contract.

### 9d. Don't forget Phoenix

`:6006` shows every query + retrieved chunk. Either keep it
SG-restricted to your IP, set `PHOENIX_ENABLE_AUTH=True` +
`PHOENIX_SECRET=…` on the phoenix container, or route it through the
same oauth2-proxy as a second upstream.

---

## 10. Gotchas

| Symptom | Fix |
|---|---|
| `CrossEncoder` download on every start | `hf-cache` not mounted / empty → re-run §4 |
| `psycopg.OperationalError: connection refused` | App raced Postgres on first boot → `make docker-restart` (or add `healthcheck`+`depends_on: condition: service_healthy`) |
| `SSL: CERTIFICATE_VERIFY_FAILED` to LiteLLM/Confluence | Set `CA_CERT=ops/corp-root-ca.crt` in `.env`, drop the cert there, `make docker-build` |
| Postgres `collation version mismatch` | `docker compose exec postgres psql -U rag -d rag -c "ALTER DATABASE rag REFRESH COLLATION VERSION"` |
| `🔍 trace` link points at `http://phoenix:6006` | Set `PHOENIX_PUBLIC_HOST=http://<ec2>:6006` (todo) — for now copy the path onto the public host |
| Watcher not firing on `data/repos` edits | Confirm dirs are real clones (not host symlinks pointing outside the mount) |
