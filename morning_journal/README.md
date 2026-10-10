# Wayland Morning Journal

A Mistral managed workflow that runs every morning, analyses the
[`gmazzo98-glitch/Wayland`](https://github.com/gmazzo98-glitch/Wayland) repository commits
and European/Italian startup & SME news feeds, and emails the
**Wayland Morning Journal** report to the configured recipients.

## Layout
- `src/workflows/journal.py` — workflow + activities (fetch commits, fetch news, write report, send email)
- `src/entrypoints/worker.py` — worker entrypoint (`run_worker`)
- `Dockerfile` — image built by Mistral managed deployments

## Email sending
Uses Gmail SMTP with an app password, bound as workspace secrets:
- `GMAIL_USER` — the sending Gmail address
- `GMAIL_APP_PASSWORD` — the 16-character Google app password

If they are not set, the workflow still generates the report and returns it in the
execution result, marking `email.sent = false`.

## Input schema
```json
{
  "recipients": ["gmazzo98@gmail.com", "giovannigatti.ita@gmail.com"],
  "repo_full_name": "gmazzo98-glitch/Wayland",
  "model": "mistral-medium-latest"
}
```
