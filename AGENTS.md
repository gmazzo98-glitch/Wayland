Before doing anything, read READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md. Before you finish, update it.

# Agent instructions

Rules for every coding agent working in this repository (Claude Code, Codex, Antigravity, others):

- The single coordination file is `READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md`: standing rules, project status, open points with a check-out protocol, decisions log, closed log.
- Edit the open points only through `python scripts/open_points.py` (run `validate` before committing the file).
- Run the tests with `python -m pytest tests --capture=no -q -p no:cacheprovider` (never bare `pytest`).
