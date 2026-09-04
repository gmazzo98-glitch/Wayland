"""
Launches the Streamlit app against an isolated, throwaway SQLite file for
local preview/testing — never the real vienna.db.

Exists because setting DATABASE_URL via a shell "set VAR=val && cmd" prefix
in .claude/launch.json proved unreliable (the shell wrapping silently
dropped the env var in this environment, so the preview server ended up
reading the real local database instead of an isolated one). A plain
runtimeExecutable=python + script argument avoids the shell entirely.
"""

import os
import sys
import tempfile
import subprocess

PREVIEW_DB_PATH = os.path.join(tempfile.gettempdir(), "vienna_preview_test.db")

if __name__ == "__main__":
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{PREVIEW_DB_PATH}"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.headless", "true", "--server.port", "8503"],
        cwd=repo_root, env=env, check=False,
    )
