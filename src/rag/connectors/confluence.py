"""
Confluence connector (Data Center, PAT auth) — version-diff incremental sync.

Config: data/connectors/confluence.yaml lists spaces. Every
sync_interval_hours, for each space:
  1. List all page_id → version.number via paginated REST.
  2. Diff against confluence_pages table.
  3. For new/changed: fetch body.storage + ancestors → markdown →
     chunk → upsert into the existing 'docs' Qdrant collection.
  4. For deleted (in table but not in remote list): delete chunks +
     row.

Idempotent on (space, page_id, chunk_idx). Reuses the 'docs'
collection so search_docs covers Confluence with no agent changes.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx
import yaml
from qdrant_client.models import (
    PointStruct, Filter, FieldCondition, MatchValue, FilterSelector,
)

from ..config import POSTGRES_DSN
from ..embed import embed_texts
from ..chunking.markdown import chunk_markdown_file
from ..retrieval.hybrid import sparse_doc
from ..ingest.docs import _client as _qdrant, ensure_docs_collection, DOCS_COLLECTION
from ..ingest.symbol_mentions import extract_mentions, repos_for_mentions
from ..indexing.graph_db import get_conn
from .storage_md import storage_to_markdown

log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get(
    "CONFLUENCE_CONFIG",
    str(Path(__file__).parents[3] / "data" / "connectors" / "confluence.yaml"),
))
_POINT_NS = uuid.UUID("c0f17e4c-e000-4a91-8a2b-1c0f17e4ce00")
_PAGE_BATCH = 100
_EMBED_BATCH = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS confluence_pages (
    space_key   text    NOT NULL,
    page_id     text    NOT NULL,
    version     integer NOT NULL,
    title       text,
    indexed_at  timestamptz DEFAULT now(),
    PRIMARY KEY (space_key, page_id)
);
CREATE TABLE IF NOT EXISTS confluence_sync (
    space_key     text PRIMARY KEY,
    last_run      timestamptz,
    pages_seen    integer DEFAULT 0,
    pages_changed integer DEFAULT 0,
    pages_deleted integer DEFAULT 0
);
"""


# ── Config ───────────────────────────────────────────────────────────

@dataclass
class SpaceConfig:
    key: str
    label_filter: list[str] = field(default_factory=list)
    include_archived: bool = False


@dataclass
class ConfluenceConfig:
    base_url: str
    auth_type: str               # "dc" (Bearer PAT) | "cloud" (Basic email:token)
    token: str
    user: str | None
    spaces: list[SpaceConfig]
    sync_interval_hours: float
    verify_ssl: bool


_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(s: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), s)


def load_config(path: Path | None = None) -> ConfluenceConfig | None:
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return None
    raw = yaml.safe_load(p.read_text()) or {}
    auth = raw.get("auth") or {}
    base = (raw.get("base_url") or "").rstrip("/")
    auth_type = (auth.get("type") or
                 ("cloud" if "atlassian.net" in base else "dc")).lower()
    token = _expand_env(str(auth.get("token") or "${CONFLUENCE_TOKEN}"))
    user = _expand_env(str(auth.get("user") or "${CONFLUENCE_USER}")) or None
    if not base or not token:
        log.warning("confluence: config at %s missing base_url or token", p)
        return None
    spaces = [
        SpaceConfig(
            key=s["key"],
            label_filter=list(s.get("label_filter") or []),
            include_archived=bool(s.get("include_archived", False)),
        )
        for s in (raw.get("spaces") or [])
        if s.get("key")
    ]
    return ConfluenceConfig(
        base_url=base,
        auth_type=auth_type,
        token=token,
        user=user,
        spaces=spaces,
        sync_interval_hours=float(raw.get("sync_interval_hours", 24)),
        verify_ssl=bool(raw.get("verify_ssl", True)),
    )


# ── REST client ──────────────────────────────────────────────────────

class ConfluenceClient:
    def __init__(self, cfg: ConfluenceConfig):
        self.cfg = cfg
        headers = {"Accept": "application/json"}
        auth = None
        if cfg.auth_type == "cloud":
            auth = (cfg.user or "", cfg.token)
        else:
            headers["Authorization"] = f"Bearer {cfg.token}"
        self._http = httpx.Client(
            base_url=f"{cfg.base_url}/rest/api",
            headers=headers,
            auth=auth,
            timeout=30.0,
            verify=cfg.verify_ssl,
        )

    def close(self) -> None:
        self._http.close()

    def list_pages(self, space: SpaceConfig) -> Iterator[dict]:
        """Yield {id, version, title} for every current page in the space.

        Uses the plain /content endpoint (spaceKey + type + status) by
        default — broadly compatible across DC versions. Falls back to
        CQL /content/search only when label_filter is set."""
        if space.label_filter:
            cql = (f'space = "{space.key}" AND type = page AND label in ('
                   + ", ".join(f'"{l}"' for l in space.label_filter) + ")")
            path = "/content/search"
            params: dict[str, Any] = {
                "cql": cql, "expand": "version",
                "limit": _PAGE_BATCH, "start": 0,
            }
        else:
            path = "/content"
            params = {
                "spaceKey": space.key, "type": "page",
                "status": ("any" if space.include_archived else "current"),
                "expand": "version",
                "limit": _PAGE_BATCH, "start": 0,
            }
        while True:
            r = self._http.get(path, params=params)
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} {r.reason_phrase} for {r.url}. "
                    f"Body: {r.text[:500]}",
                    request=r.request, response=r,
                )
            data = r.json()
            for item in data.get("results", []):
                yield {
                    "id": str(item["id"]),
                    "version": int((item.get("version") or {}).get("number", 1)),
                    "title": item.get("title", ""),
                }
            if data.get("size", 0) < params["limit"]:
                break
            params["start"] += params["limit"]

    def get_page(self, page_id: str) -> dict:
        r = self._http.get(
            f"/content/{page_id}",
            params={"expand": "body.storage,version,ancestors,space"},
        )
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{r.status_code} {r.reason_phrase} for {r.url}. "
                f"Body: {r.text[:500]}",
                request=r.request, response=r,
            )
        d = r.json()
        return {
            "id": str(d["id"]),
            "version": int(d["version"]["number"]),
            "title": d.get("title", ""),
            "space": (d.get("space") or {}).get("key", ""),
            "ancestors": [a.get("title", "") for a in (d.get("ancestors") or [])],
            "storage": ((d.get("body") or {}).get("storage") or {}).get("value", ""),
            "url": f"{self.cfg.base_url}{(d.get('_links') or {}).get('webui', '')}",
        }


# ── State (Postgres) ─────────────────────────────────────────────────

def ensure_schema() -> None:
    with get_conn().cursor() as cur:
        cur.execute(_SCHEMA)


def _local_versions(space_key: str) -> dict[str, int]:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT page_id, version FROM confluence_pages WHERE space_key = %s",
            (space_key,),
        )
        return {r["page_id"]: r["version"] for r in cur.fetchall()}


def _record_page(space_key: str, page_id: str, version: int,
                 title: str) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            """INSERT INTO confluence_pages (space_key, page_id, version,
                                              title, indexed_at)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (space_key, page_id)
               DO UPDATE SET version = EXCLUDED.version,
                             title = EXCLUDED.title,
                             indexed_at = now()""",
            (space_key, page_id, version, title),
        )


def _delete_page_state(space_key: str, page_id: str) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM confluence_pages WHERE space_key = %s AND page_id = %s",
            (space_key, page_id),
        )


def _record_sync(space_key: str, seen: int, changed: int,
                 deleted: int) -> None:
    with get_conn().cursor() as cur:
        cur.execute(
            """INSERT INTO confluence_sync (space_key, last_run, pages_seen,
                                             pages_changed, pages_deleted)
               VALUES (%s, now(), %s, %s, %s)
               ON CONFLICT (space_key)
               DO UPDATE SET last_run = now(),
                             pages_seen = EXCLUDED.pages_seen,
                             pages_changed = EXCLUDED.pages_changed,
                             pages_deleted = EXCLUDED.pages_deleted""",
            (space_key, seen, changed, deleted),
        )


def last_sync(space_key: str) -> dict | None:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT * FROM confluence_sync WHERE space_key = %s",
            (space_key,),
        )
        return cur.fetchone()


# ── Indexing ─────────────────────────────────────────────────────────

def _delete_page_chunks(space_key: str, page_id: str) -> None:
    _qdrant().delete(
        collection_name=DOCS_COLLECTION,
        points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="source", match=MatchValue(value="confluence")),
            FieldCondition(key="space", match=MatchValue(value=space_key)),
            FieldCondition(key="page_id", match=MatchValue(value=page_id)),
        ])),
        wait=True,
    )


def _index_page(page: dict) -> int:
    space = page["space"]
    page_id = page["id"]
    breadcrumb = " > ".join(p for p in page["ancestors"] if p)
    md = storage_to_markdown(page["storage"])
    if not md.strip():
        return 0

    title_line = (f"# {breadcrumb} > {page['title']}"
                  if breadcrumb else f"# {page['title']}")
    full = f"{title_line}\n\n{md}"

    rel = f"confluence/{space}/{page_id}"
    chunks = chunk_markdown_file(rel, full, repo="docs")

    mentions = extract_mentions(full)
    relates_to = repos_for_mentions(mentions)

    _delete_page_chunks(space, page_id)

    n = 0
    for i in range(0, len(chunks), _EMBED_BATCH):
        batch = chunks[i:i + _EMBED_BATCH]
        dense = embed_texts([c["text"] for c in batch])
        points = []
        for j, (ch, dv) in enumerate(zip(batch, dense)):
            idx = i + j
            meta = ch["metadata"]
            payload = {
                "text": ch["text"],
                "source": "confluence",
                "space": space,
                "page_id": page_id,
                "page_version": page["version"],
                "title": page["title"],
                "url": page["url"],
                "filepath": rel,
                "section_path": meta.get("section_path", ""),
                "breadcrumb": breadcrumb,
                "chunk_idx": idx,
                "chunk_type": meta.get("chunk_type", "markdown_section"),
            }
            if relates_to:
                payload["relates_to"] = relates_to
            cm = [m for m in mentions if m in ch["text"]]
            if cm:
                payload["mentions_symbols"] = cm
            pid = str(uuid.uuid5(
                _POINT_NS, f"confluence|{space}|{page_id}|{idx}"))
            points.append(PointStruct(
                id=pid,
                vector={"dense": dv, "sparse": sparse_doc(ch["text"])},
                payload=payload,
            ))
        _qdrant().upsert(collection_name=DOCS_COLLECTION, points=points)
        n += len(points)
    return n


# ── Sync ─────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    space: str
    seen: int = 0
    changed: int = 0
    deleted: int = 0
    chunks: int = 0
    duration_s: float = 0.0
    error: str | None = None


def sync_space(client: ConfluenceClient, space: SpaceConfig,
               *, force: bool = False) -> SyncResult:
    t0 = time.perf_counter()
    res = SyncResult(space=space.key)
    ensure_schema()
    ensure_docs_collection()

    try:
        remote: dict[str, dict] = {
            p["id"]: p for p in client.list_pages(space)
        }
    except Exception as e:
        res.error = f"list_pages: {type(e).__name__}: {e}"
        log.error("confluence[%s] %s", space.key, res.error)
        return res

    local = _local_versions(space.key)
    res.seen = len(remote)

    changed_ids = [
        pid for pid, p in remote.items()
        if force or local.get(pid, -1) < p["version"]
    ]
    deleted_ids = [pid for pid in local if pid not in remote]

    log.info("confluence[%s] seen=%d changed=%d deleted=%d",
             space.key, res.seen, len(changed_ids), len(deleted_ids))

    for pid in changed_ids:
        try:
            page = client.get_page(pid)
            n = _index_page(page)
            _record_page(space.key, pid, page["version"], page["title"])
            res.changed += 1
            res.chunks += n
            log.info("  [%s] %s v%d → %d chunks",
                     space.key, page["title"][:60], page["version"], n)
        except Exception as e:
            log.warning("  [%s] page %s failed: %s", space.key, pid, e)

    for pid in deleted_ids:
        _delete_page_chunks(space.key, pid)
        _delete_page_state(space.key, pid)
        res.deleted += 1

    _record_sync(space.key, res.seen, res.changed, res.deleted)
    res.duration_s = time.perf_counter() - t0
    return res


def sync_all(*, force: bool = False,
             only_space: str | None = None) -> list[SyncResult]:
    cfg = load_config()
    if not cfg:
        log.info("confluence: no config at %s — skipping", CONFIG_PATH)
        return []
    client = ConfluenceClient(cfg)
    try:
        out = []
        for sp in cfg.spaces:
            if only_space and sp.key != only_space:
                continue
            out.append(sync_space(client, sp, force=force))
        return out
    finally:
        client.close()


def delete_space(space_key: str) -> dict:
    """Remove all chunks + state for a space. Next sync will re-index
    everything from scratch."""
    ensure_schema()
    n_chunks = 0
    if _qdrant().collection_exists(DOCS_COLLECTION):
        flt = Filter(must=[
            FieldCondition(key="source", match=MatchValue(value="confluence")),
            FieldCondition(key="space", match=MatchValue(value=space_key)),
        ])
        n_chunks = _qdrant().count(
            DOCS_COLLECTION, count_filter=flt, exact=True).count
        _qdrant().delete(
            collection_name=DOCS_COLLECTION,
            points_selector=FilterSelector(filter=flt),
            wait=True,
        )
    with get_conn().cursor() as cur:
        cur.execute(
            "DELETE FROM confluence_pages WHERE space_key = %s", (space_key,))
        n_pages = cur.rowcount
        cur.execute(
            "DELETE FROM confluence_sync WHERE space_key = %s", (space_key,))
    log.info("confluence[%s] reset: %d pages, %d chunks removed",
             space_key, n_pages, n_chunks)
    return {"space": space_key, "pages": n_pages, "chunks": n_chunks}


def sync_status() -> list[dict]:
    ensure_schema()
    with get_conn().cursor() as cur:
        cur.execute("""
            SELECT s.space_key, s.last_run, s.pages_seen, s.pages_changed,
                   s.pages_deleted,
                   (SELECT count(*) FROM confluence_pages p
                    WHERE p.space_key = s.space_key) AS pages_indexed
            FROM confluence_sync s ORDER BY s.space_key
        """)
        return list(cur.fetchall())
