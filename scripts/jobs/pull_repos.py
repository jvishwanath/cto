import subprocess
from pathlib import Path
from rag.config import REPOS_DIR

for d in sorted(Path(REPOS_DIR).iterdir()):
    if (d / ".git").exists():
        r = subprocess.run(["git", "-C", str(d), "pull", "--ff-only"],
                             capture_output=True, text=True)
        mark = "✓" if r.returncode == 0 else "✗"
        print(f"{mark} {d.name}: {(r.stdout or r.stderr).strip()[:120]}")