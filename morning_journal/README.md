# Wayland Morning Journal (automated daily email)

A GitHub Actions workflow (free for this public repository) that runs every morning,
analyses the latest Wayland commits and European/Italian startup & SME news feeds,
and emails **The Wayland Morning Journal** to the configured recipients.

- `morning_journal/generate_journal.py` — standalone generator (stdlib only, no dependencies)
- `.github/workflows/wayland-morning-journal.yml` — daily schedule (07:30 Europe/Rome) + manual trigger

## How email sending works

Sending goes through **Zoho Mail SMTP** from `giorgio@verdantex.io` — the same
provider, host (`smtppro.zoho.eu`), port fallbacks (587 STARTTLS / 465 SSL) and
app-password mechanism used by the Email-Automatizer repository.

## One-time setup (repo secrets)

Add these under **Settings > Secrets and variables > Actions → Secrets**:

| Secret | Value |
|---|---|
| `MISTRAL_API_KEY` | API key from console.mistral.ai (free tier works) |
| `ZOHO_APP_PASSWORD` | the Zoho app password for giorgio@verdantex.io — same one already used by Email-Automatizer (if you need a new one: accounts.zoho.eu → Security → App Passwords) |

Optional repo **Variables** (not secrets): `ZOHO_EMAIL` (default `giorgio@verdantex.io`),
`JOURNAL_MODEL` (default `mistral-small-latest`).

## Test it

Actions tab > "Wayland Morning Journal" > "Run workflow" > Run — the email should
arrive in ~1 minute, sent from giorgio@verdantex.io to gmazzo98@gmail.com and
giovannigatti.ita@gmail.com.
