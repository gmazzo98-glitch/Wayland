# Wayland Morning Journal (automated daily email)

A GitHub Actions workflow (free for this public repository) that runs every morning,
analyses the latest Wayland commits and European/Italian startup & SME news feeds,
and emails **The Wayland Morning Journal** to the configured recipients.

- `morning_journal/generate_journal.py` — standalone generator (stdlib only, no dependencies)
- `.github/workflows/wayland-morning-journal.yml` — daily schedule (07:30 Europe/Rome) + manual trigger

## One-time setup (repo secrets)

Add these under **Settings > Secrets and variables > Actions > Secrets**:

| Secret | Value |
|---|---|
| `MISTRAL_API_KEY` | An API key from console.mistral.ai (free tier works, uses mistral-small-latest by default) |
| `GMAIL_APP_PASSWORD` | A Google App Password for gmazzo98@gmail.com (myaccount.google.com > Security > 2-Step Verification > App passwords) |

Optional secret `GMAIL_USER` (defaults to gmazzo98@gmail.com), optional repo variable `JOURNAL_MODEL`.

## Test it

Actions tab > "Wayland Morning Journal" > "Run workflow" > Run — the email should arrive in ~1 minute.
