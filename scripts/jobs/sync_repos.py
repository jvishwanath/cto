# scripts/jobs/sync_repos.py
import sys
import yaml
import logging
import subprocess
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from rag.config import REPOS_DIR

log = logging.getLogger(__name__)

def load_config(config_path: Path) -> list[dict]:
    if not config_path.exists():
        log.warning(f"Config file not found: {config_path}")
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data.get("repos", []) if isinstance(data, dict) else []
    except Exception as e:
        log.error(f"Failed to parse {config_path}: {e}")
        return []

def sync_repository(name: str, url: str, repo_dir: Path) -> Tuple[str, list[str] | None, set[str] | None]:
    """
    Synchronizes a repository.
    Returns: (status, changed_files, deleted_files)
    status: 'cloned', 'up-to-date', 'updated', 'error'
    """
    if not repo_dir.exists():
        log.info(f"Cloning {name} from {url}...")
        r = subprocess.run(["git", "clone", url, str(repo_dir)], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            log.error(f"Clone failed for {name}: {r.stderr}")
            return "error", None, None
        return "cloned", None, None

    # Exists, do pull
    log.info(f"Updating {name}...")
    
    r_old = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    old_hash = r_old.stdout.strip() if r_old.returncode == 0 else ""

    r_pull = subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], capture_output=True, text=True)
    if r_pull.returncode != 0:
        log.error(f"Pull failed for {name}: {r_pull.stderr}")
        return "error", None, None

    r_new = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    new_hash = r_new.stdout.strip() if r_new.returncode == 0 else ""

    if old_hash and new_hash and old_hash == new_hash:
        return "up-to-date", [], set()

    # Hashes changed, parse diff
    if old_hash and new_hash:
        r_diff = subprocess.run(["git", "-C", str(repo_dir), "diff", "--name-status", old_hash, new_hash], capture_output=True, text=True)
        if r_diff.returncode == 0:
            changed, deleted = [], set()
            for line in r_diff.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split('\t', 1)
                if len(parts) != 2:
                    continue
                status, file_path = parts[0].strip(), parts[1].strip()
                if status.startswith('D'):
                    deleted.add(file_path)
                else:
                    changed.append(file_path)
            return "updated", changed, deleted
            
    return "updated", None, None

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    config_path = Path("data/repos.yaml")
    repos_list = load_config(config_path)
    
    if not repos_list:
        log.info("No repositories configured in data/repos.yaml")
        return 0

    base_dir = Path(REPOS_DIR)
    base_dir.mkdir(parents=True, exist_ok=True)

    for repo_cfg in repos_list:
        name = repo_cfg.get("name")
        url = repo_cfg.get("url")
        if not name or not url:
            log.warning(f"Invalid repo config: {repo_cfg}")
            continue
            
        repo_dir = base_dir / name
        status, _, _ = sync_repository(name, url, repo_dir)
        
        if status == "error":
            log.error(f"{name}: Failed to sync.")
        elif status == "cloned":
            log.info(f"{name}: Successfully cloned.")
        elif status == "updated":
            log.info(f"{name}: Successfully updated.")
        else:
            log.info(f"{name}: Up to date.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
