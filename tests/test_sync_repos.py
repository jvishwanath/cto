# tests/test_sync_repos.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from scripts.jobs.sync_repos import load_config, sync_repository

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
