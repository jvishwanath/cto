"""
Manual Confluence sync. Reads data/connectors/confluence.yaml.

  python scripts/confluence_sync.py            # all configured spaces, version-diff
  python scripts/confluence_sync.py --space K  # one space
  python scripts/confluence_sync.py --force    # re-index regardless of stored version
  python scripts/confluence_sync.py --status   # show last-sync state per space
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rag.connectors.confluence import (
    load_config, sync_all, sync_status, delete_space, CONFIG_PATH,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", help="Sync only this space key")
    ap.add_argument("--force", action="store_true",
                    help="Re-index all pages regardless of stored version")
    ap.add_argument("--status", action="store_true",
                    help="Print per-space last-sync state and exit")
    ap.add_argument("--reset", metavar="SPACE",
                    help="Delete all chunks + state for SPACE and exit "
                         "(next sync re-indexes from scratch)")
    args = ap.parse_args()

    if args.reset:
        r = delete_space(args.reset)
        print(f"  reset {r['space']}: {r['pages']} pages, "
              f"{r['chunks']} chunks removed")
        return 0

    if args.status:
        rows = sync_status()
        if not rows:
            print("(no sync state recorded)")
            return 0
        for r in rows:
            print(f"  {r['space_key']:12} last_run={r['last_run']}  "
                  f"indexed={r['pages_indexed']:5d}  seen={r['pages_seen']:5d}  "
                  f"changed={r['pages_changed']:4d}  deleted={r['pages_deleted']:3d}")
        return 0

    cfg = load_config()
    if not cfg:
        print(f"No config at {CONFIG_PATH} (or missing base_url/token).")
        print(f"Copy data/connectors/confluence.yaml.example → "
              f"confluence.yaml and set CONFLUENCE_TOKEN in .env.")
        return 1
    if not cfg.spaces:
        print("Config has no spaces listed.")
        return 1

    results = sync_all(force=args.force, only_space=args.space)
    if args.space and not results:
        print(f"Space {args.space!r} not in config "
              f"({[s.key for s in cfg.spaces]}).")
        return 1

    print()
    rc = 0
    for r in results:
        line = (f"  {r.space:12} seen={r.seen:5d}  changed={r.changed:4d}  "
                f"deleted={r.deleted:3d}  chunks={r.chunks:5d}  "
                f"{r.duration_s:6.1f}s")
        if r.error:
            line += f"  ERROR: {r.error}"
            rc = 2
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
