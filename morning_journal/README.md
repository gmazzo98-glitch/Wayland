# Wayland Morning Journal (automated daily email)

A GitHub Actions workflow (free for this public repository) that runs every morning,
analyses the latest Wayland commits and European/Italian startup & SME news feeds,
and emails **The Wayland Morning Journal** to the configured recipients.

- `morning_journal/generate_journal.py` — standalone generator (stdlib only, no dependencies)
- `morning_journal/gmail_oauth_setup.py` — one-time OAuth setup producing the Gmail refresh token
- `.github/workflows/wayland-morning-journal.yml` — daily schedule (07:30 Europe/Rome) + manual trigger

## How email sending works (no App Password needed)

Sending uses the **official Gmail API with OAuth** — the same approach as the
Email-Automatizer repository. Google App Passwords are not required (and are not
available on some accounts by design). You authorize once, GitHub Actions renews
access automatically forever after.

## One-time setup

### A) Gmail OAuth credentials (Google Cloud Console, free)

1. console.cloud.google.com → create or pick a project
2. **APIs & Services → Library** → enable **Gmail API**
3. **APIs & Services → OAuth consent screen** → External → fill required fields →
   **Publish to Production** (so refresh tokens don't expire after 7 days) →
   add gmazzo98@gmail.com as a test user
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   type **Desktop app** → copy Client ID and Client secret

### B) Generate the refresh token (once, on your PC)

```bash
python morning_journal/gmail_oauth_setup.py
```

Paste the Client ID and secret, sign into gmazzo98@gmail.com in the browser,
click **Advanced → Go to app** on the unverified warning, approve — the script
prints the refresh token.

### C) Add repo secrets

Under **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Value |
|---|---|
| `MISTRAL_API_KEY` | API key from console.mistral.ai (free tier works) |
| `GMAIL_CLIENT_ID` | from step A4 |
| `GMAIL_CLIENT_SECRET` | from step A4 |
| `GMAIL_REFRESH_TOKEN` | printed by the setup script |

## Test it

Actions tab > "Wayland Morning Journal" > "Run workflow" > Run — the email should arrive in ~1 minute.

If the OAuth consent screen is left in "Testing" mode, Google expires the refresh
token after 7 days and sending breaks — keep the app published to Production.
