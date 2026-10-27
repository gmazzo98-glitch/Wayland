Before doing anything, read READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md. Before you finish, update it.

# Agent instructions

Rules for every coding agent working in this repository (Claude Code, Codex, Antigravity, others):

- The single coordination file is `READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md`: standing rules, project status, open points with a check-out protocol, decisions log, closed log.
- Edit the open points only through `python scripts/open_points.py` (run `validate` before committing the file).
- Run the tests with `python -m pytest tests --capture=no -q -p no:cacheprovider` (never bare `pytest`).

## Landing page (wayland.my), folder `landing/`

Separate from the Vienna app. Read `landing/README.md` before touching it.

- Edit `landing/src/page.html` (readable HTML, CSS and JS), never the escaped single line inside `landing/index.html`. Then run `python landing/tools/pack.py` and commit both files. `python landing/tools/pack.py --check` must say "In sync".
- Keep the form field names and the POST to `/api/submit`: company `fullName, companyName, workEmail, painPoints`; startup `startupName, website, workEmail, pitch, dataNeeded`; both also send `termsAccepted: true` and `termsVersion` (the required terms checkbox, stored in Neon). The page must link `/privacy` and `/terms`. `pack.py` refuses a page that lost any of this.
- The legal texts live only in `landing/privacy/index.html` and `landing/terms/index.html`. Do not copy them into the page, do not load fonts or scripts from other sites, and do not invent legal facts: unknown company details stay as visible placeholders for Giorgio. If a text changes in substance, bump the version and date in both pages and `LEGAL_VERSION` in `src/page.html` together (see `landing/README.md`). A new, renamed or removed field also needs `landing/api/submit.js` and the Neon tables `landing.company_requests` / `landing.startup_proposals` changed: stop and ask Giorgio first.
- Never put secrets in the repo. `DATABASE_URL` and `NTFY_TOPIC` live only in the Vercel project settings; you do not need them to edit the page.
- Do not bring back the copy Giorgio had removed: "Niente dati, niente pilot...", "Non tutte le proposte vengono abbinate" (the form note and the confirmation paragraph), and any "mockup" notes.
- Do not run SQL against the `landing` tables without Giorgio's go-ahead.
- Pushing does not publish by itself unless the Vercel project is connected to this repo (see `landing/README.md`). Do not deploy by other means; say in your final message whether the change is live.
- Update `READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md` (the "Landing page" bullet in section B) after a change that affects what is live.
