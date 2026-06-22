# Scheduled Repo Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a background job script (`sync_repos.py`) that reads a YAML config, clones new git repositories, pulls updates for existing ones, and triggers the incremental RAG indexer.

**Architecture:** A standalone Python script running as an `exec` job on the `host` network. It uses the `subprocess` module to execute native `git` commands (leveraging host SSH auth), parses `git rev-parse` and `git diff` outputs, and invokes the `index_repo` Python function from `rag.ingest.incremental`.

**Tech Stack:** Python 3, PyYAML, Git CLI.

---

### Task 1: Create Test Fixtures & Basic YAML Parser

**Files:**
- Create: `tests/test_sync_repos.py`
- Create: `scripts/jobs/sync_repos.py`

- [ ] **Step 1: Write the failing test for YAML parsing**
```python
# tests/test_sync_repos.py
import pytest
from pathlib import Path
from scripts.jobs.sync_repos import load_config

def test_load_config_valid(tmp_path):
    yaml_file = tmp_path / "repos.yaml"
    yaml_file.write_text('''
repos:
  - name: test-repo
    url: git@github.com:test/test-repo.git
''')
    config = load_config(yaml_file)
    assert len(config) == 1
    assert config[0]["name"] == "test-repo"
    assert config[0]["url"] == "git@github.com:test/test-repo.git"

def test_load_config_missing_file(tmp_path):
    assert load_config(tmp_path / "missing.yaml") == []
```

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_sync_repos.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'scripts'"

- [ ] **Step 3: Write minimal implementation**
```python
# scripts/jobs/sync_repos.py
import yaml
import logging
from pathlib import Path

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
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=. pytest tests/test_sync_repos.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add tests/test_sync_repos.py scripts/jobs/sync_repos.py
git commit -m "feat(sync): add yaml config parsing for repo sync"
```

---

### Task 2: Implement Git Clone & Pull Logic

**Files:**
- Modify: `tests/test_sync_repos.py`
- Modify: `scripts/jobs/sync_repos.py`

- [ ] **Step 1: Write the failing tests for Git operations**
```python
# tests/test_sync_repos.py (append)
from unittest.mock import patch, MagicMock
from scripts.jobs.sync_repos import sync_repository

@patch("scripts.jobs.sync_repos.subprocess.run")
def test_sync_repository_clone_new(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0)
    
    # repo dir doesn't exist yet
    repo_dir = tmp_path / "new-repo"
    
    status, files, deleted = sync_repository("new-repo", "git@github.com:new.git", repo_dir)
    
    assert status == "cloned"
    assert files is None
    assert deleted is None
    mock_run.assert_called_with(["git", "clone", "git@github.com:new.git", str(repo_dir)], capture_output=True, text=True, check=False)

@patch("scripts.jobs.sync_repos.subprocess.run")
def test_sync_repository_pull_no_changes(mock_run, tmp_path):
    # repo dir exists
    repo_dir = tmp_path / "existing-repo"
    repo_dir.mkdir()
    
    # Mock sequence: rev-parse (old), pull, rev-parse (new identical)
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="abc1234\n"),
        MagicMock(returncode=0),
        MagicMock(returncode=0, stdout="abc1234\n")
    ]
    
    status, files, deleted = sync_repository("existing-repo", "git@...", repo_dir)
    assert status == "up-to-date"
    assert files == []
    assert deleted == set()
```

- [ ] **Step 2: Run test to verify it fails**
Run: `PYTHONPATH=. pytest tests/test_sync_repos.py::test_sync_repository_clone_new -v`
Expected: FAIL with "ImportError: cannot import name 'sync_repository'"

- [ ] **Step 3: Write minimal implementation**
```python
# scripts/jobs/sync_repos.py (append)
import subprocess
from typing import Tuple

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
        
    return "updated", [], set()  # Diffing implemented in next task
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=. pytest tests/test_sync_repos.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add tests/test_sync_repos.py scripts/jobs/sync_repos.py
git commit -m "feat(sync): add git clone and pull logic"
```

---

### Task 3: Implement Git Diff Parsing

**Files:**
- Modify: `tests/test_sync_repos.py`
- Modify: `scripts/jobs/sync_repos.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/test_sync_repos.py (append)
@patch("scripts.jobs.sync_repos.subprocess.run")
def test_sync_repository_pull_with_changes(mock_run, tmp_path):
    repo_dir = tmp_path / "changed-repo"
    repo_dir.mkdir()
    
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="oldhash\n"),
        MagicMock(returncode=0),
        MagicMock(returncode=0, stdout="newhash\n"),
        MagicMock(returncode=0, stdout="M\tfile1.py\nA\tfile2.py\nD\tfile3.py\n")
    ]
    
    status, files, deleted = sync_repository("changed-repo", "git@...", repo_dir)
    assert status == "updated"
    assert set(files) == {"file1.py", "file2.py"}
    assert deleted == {"file3.py"}
    
    # Verify the diff call
    mock_run.assert_called_with(["git", "-C", str(repo_dir), "diff", "--name-status", "oldhash", "newhash"], capture_output=True, text=True)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `PYTHONPATH=. pytest tests/test_sync_repos.py::test_sync_repository_pull_with_changes -v`
Expected: FAIL (returns empty lists instead of parsed files)

- [ ] **Step 3: Write minimal implementation**
```python
# scripts/jobs/sync_repos.py (modify sync_repository block)
    # ... previous code inside sync_repository ...
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
            
    return "updated", None, None # Fallback to full index if diff fails
```

- [ ] **Step 4: Run test to verify it passes**
Run: `PYTHONPATH=. pytest tests/test_sync_repos.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add tests/test_sync_repos.py scripts/jobs/sync_repos.py
git commit -m "feat(sync): parse git diff name-status for incremental updates"
```

---

### Task 4: Tie it together into Main Loop

**Files:**
- Modify: `scripts/jobs/sync_repos.py`
- Modify: `data/schedules.yaml.example`

- [ ] **Step 1: Write the main execution wrapper**
```python
# scripts/jobs/sync_repos.py (append)
import os
import sys

# Assume we run from project root or virtual env
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from rag.config import REPOS_DIR
from rag.ingest.incremental import index_repo

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
        status, changed_files, deleted_files = sync_repository(name, url, repo_dir)
        
        if status == "error":
            continue
        elif status == "up-to-date":
            log.info(f"{name}: No changes.")
            continue
        elif status == "cloned" or changed_files is None:
            log.info(f"{name}: Triggering full index...")
            index_repo(name, files=None)
        elif status == "updated":
            log.info(f"{name}: Triggering incremental index ({len(changed_files)} changed, {len(deleted_files)} deleted)...")
            index_repo(name, files=changed_files, deleted=deleted_files)

    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add example wiring to schedules**
```yaml
# data/schedules.yaml.example (append)
  - name: sync-repos
    interval_min: 60
    exec: scripts/jobs/sync_repos.py
    exec_mode: host
    on_complete: [notify]
    notify: log://
```

- [ ] **Step 3: Commit**
```bash
git add scripts/jobs/sync_repos.py data/schedules.yaml.example
git commit -m "feat(sync): add main loop calling incremental indexer"
```