"""
Standalone filesystem watcher.
Usage: python scripts/watch.py
Drop a repo into data/repos/ or a PDF/MD into data/docs/ → auto-indexed.
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rag.ingest.watcher import IngestWatcher


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    w = IngestWatcher().start()
    print("Watching data/repos/ and data/docs/ — Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        w.stop()


if __name__ == "__main__":
    main()
