#!/usr/bin/env python3
"""Wayland Morning Journal — daily generator.

Fetches the latest commits of the Wayland repository and European/Italian
startup & SME news feeds, has a Mistral model write the daily report, and
emails it to the configured recipients through Zoho Mail (Verdantex address),
mirroring the Email-Automatizer's zoho_service.py.

Environment variables (GitHub Actions secrets):
  MISTRAL_API_KEY      - Mistral API key (console.mistral.ai)
  ZOHO_APP_PASSWORD    - Zoho Mail app password for the Verdantex account

Optional:
  ZOHO_EMAIL           - sender (default: giorgio@verdantex.io)
  JOURNAL_RECIPIENTS   - comma-separated (default: gmazzo98@gmail.com,giovannigatti.ita@gmail.com)
  JOURNAL_REPO         - repo full name (default: gmazzo98-glitch/Wayland)
  JOURNAL_MODEL        - Mistral model (default: mistral-small-latest)
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Same host candidates as Email-Automatizer's check_zoho_connection()
ZOHO_SMTP_HOSTS = ["smtppro.zoho.eu", "smtp.zoho.eu", "smtppro.zoho.com", "smtp.zoho.com"]
ZOHO_SMTP_PORTS = [587, 465]

RSS_FEEDS = [
    ("EU-Startups", "https://www.eu-startups.com/feed/"),
    ("Tech.eu", "https://tech.eu/feed/"),
    ("Startupbusiness (IT)", "https://www.startupbusiness.it/feed/"),
]


def summary(text: str) -> None:
    """Append a diagnostic line to the GitHub Actions step summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:
        pass


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wayland-morning-journal/1.2", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_commits(repo_full_name: str, per_page: int = 30, token: str = "") -> list[dict]:
    url = f"https://api.github.com/repos/{repo_full_name}/commits?per_page={per_page}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.loads(http_get(url, headers=headers))
    commits = []
    for c in data:
        commits.append({
            "sha": c["sha"][:8],
            "date": c["commit"]["author"]["date"],
            "author": c["commit"]["author"]["name"],
            "message": c["commit"]["message"][:1500],
            "url": c["html_url"],
        })
    return commits


def fetch_news(max_days_back: int = 5, max_items_per_feed: int = 12) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days_back)
    items: list[dict] = []
    for source, feed_url in RSS_FEEDS:
        try:
            root = ElementTree.fromstring(http_get(feed_url))
        except Exception as exc:
            items.append({"source": source, "title": f"[feed unavailable: {exc.__class__.__name__}]",
                          "description": "", "link": feed_url, "date": ""})
            continue
        count = 0
        for item in root.iter("item"):
            if count >= max_items_per_feed:
                break
            pub = (item.findtext("pubDate") or "").strip()
            try:
                when = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
                if when < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            items.append({
                "source": source,
                "title": (item.findtext("title") or "").strip(),
                "description": strip_html(item.findtext("description") or "")[:400],
                "link": (item.findtext("link") or "").strip(),
                "date": pub,
            })
            count += 1
    return items


def write_report(commits: list[dict], news: list[dict], model: str, recipients: list[str], api_key: str) -> dict:
    now_rome = datetime.now(ROME)
    subject = f"The Wayland Morning Journal {now_rome:%d.%m.%y}"

    commits_text = "\n\n".join(
        f"- [{c['date']}] {c['sha']} by {c['author']}: {c['message']}" for c in commits
    ) or "(no commits found)"
    news_text = "\n\n".join(
        f"- [{n['date']}] ({n['source']}) {n['title']} — {n['description']} Link: {n['link']}" for n in news
    ) or "(no news items found)"

    prompt = f"""Today is {now_rome:%d %B %Y}. You are writing "The Wayland Morning Journal", a daily email report about the Wayland repository (Project Vienna: a Streamlit company-screening dashboard that sources public signals, scores them into indicators, and surfaces pain points and valuations, focused on Italian SMEs) and about the Italian and European startup & SME ecosystem.

The email subject is exactly: "{subject}"

Using the data below, write the report in English, structured in three sections:

1) REPOSITORY UPDATES — was the code updated? Explain the new main functionalities in plain language, each as a short list entry with: what it does, why it matters, a short tutorial/how-to-use note, and its capabilities. If no commits are new, say so briefly and summarize the most recent ones.

2) SCOPE ANALYSIS & ECOSYSTEM DIGEST — first silently analyse the scope of the Wayland tool and which startup categories could provide innovative solutions to SMEs matched by this data layer (GovTech/B2G intelligence, vertical AI/SaaS for SME digitization, M&A/succession advisory, SME credit-scoring fintech, etc.); then a digest of the news and pains of the Italian and European startup and SME ecosystem based on the news items provided. Mention concrete facts, numbers and dates when present in the data.

3) POINTS OF VIEW & OUTLOOKS — interesting perspectives on the situation and possible outlooks for the Wayland tool and project, including risks and next steps.

Style: clear, direct, business-savvy, no fluff, no invented facts — use only the data provided, and clearly mark anything uncertain.

GITHUB COMMITS (newest first):
{commits_text}

NEWS ITEMS:
{news_text}

The report will be emailed to {", ".join(recipients)}.
Output ONLY the HTML for the email body: a single <div> container (no <html>/<head>/<body>), using <h2> for the three section titles, <h3> and <ul>/<li> for entries, <strong> for emphasis. No inline CSS styles, no <script>, no images."""

    body = json.dumps({
        "model": model,
        "temperature": 0.4,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        MISTRAL_API_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode("utf-8"))
    html = (resp["choices"][0]["message"]["content"] or "").strip()
    if html.startswith("```"):
        fence = "```"
        for prefix in (fence + "html\n", fence + "\n", fence + "html", fence):
            if html.startswith(prefix):
                html = html[len(prefix):]
                break
        if html.endswith(fence):
            html = html[:-3]
        html = html.strip()
    return {"html": html, "subject": subject}


def send_via_zoho(report: dict, recipients: list[str], sender: str, password: str) -> dict:
    """Send the report through Zoho Mail SMTP (same settings as Email-Automatizer)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = report["subject"]
    msg["From"] = f"Wayland Journal <{sender}>"
    msg["To"] = ", ".join(recipients)
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;color:#1a1a1a;line-height:1.5">'
        f'<h1 style="border-bottom:3px solid #2b6cb0;padding-bottom:8px">{escape(report["subject"])}</h1>'
        f"{report['html']}"
        "</div>"
    )
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", html), "plain"))
    msg.attach(MIMEText(html, "html"))

    password = password.replace(" ", "").strip()  # Zoho app passwords may contain spaces
    last_error = None
    for host in ZOHO_SMTP_HOSTS:
        for port in ZOHO_SMTP_PORTS:
            try:
                if port == 465:
                    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                        server.login(sender, password)
                        server.sendmail(sender, recipients, msg.as_string())
                else:
                    with smtplib.SMTP(host, port, timeout=30) as server:
                        server.ehlo()
                        server.starttls()
                        server.ehlo()
                        server.login(sender, password)
                        server.sendmail(sender, recipients, msg.as_string())
                return {"sent": True, "via": f"Zoho SMTP ({host}:{port})", "subject": report["subject"],
                        "recipients": recipients}
            except smtplib.SMTPAuthenticationError:
                raise  # wrong password: no point trying other hosts
            except Exception as exc:
                last_error = f"{host}:{port} → {exc.__class__.__name__}: {exc}"
                continue
    raise RuntimeError(f"All Zoho SMTP hosts failed. Last error: {last_error}")


def main() -> int:
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    zoho_password = os.environ.get("ZOHO_APP_PASSWORD", "").strip()
    sender = os.environ.get("ZOHO_EMAIL", "giorgio@verdantex.io").strip()
    recipients = [r.strip() for r in os.environ.get(
        "JOURNAL_RECIPIENTS", "gmazzo98@gmail.com,giovannigatti.ita@gmail.com").split(",") if r.strip()]
    repo = os.environ.get("JOURNAL_REPO", "gmazzo98-glitch/Wayland").strip()
    model = os.environ.get("JOURNAL_MODEL", "mistral-small-latest").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    summary("- MISTRAL_API_KEY: " + ("present" if api_key else "MISSING"))
    summary("- ZOHO_APP_PASSWORD: " + ("present" if zoho_password else "MISSING"))

    missing = [name for name, val in (("MISTRAL_API_KEY", api_key),
                                      ("ZOHO_APP_PASSWORD", zoho_password)) if not val]
    if missing:
        print(f"ERROR: missing required secrets: {', '.join(missing)}. "
              f"Add them under Settings > Secrets and variables > Actions.")
        summary("**FAILED: missing secrets: " + ", ".join(missing) + "**")
        return 1

    try:
        commits = fetch_commits(repo, token=github_token)
        news = fetch_news()
        print(f"Fetched {len(commits)} commits and {len(news)} news items.")
        summary(f"- Fetched {len(commits)} commits and {len(news)} news items")
        report = write_report(commits, news, model, recipients, api_key)
        status = send_via_zoho(report, recipients, sender, zoho_password)
    except Exception:
        summary("FAILED with unhandled error:\n\n```\n" + traceback.format_exc() + "\n```")
        raise
    print(f"Email status: {status}")
    summary(f"- **Sent via {status['via']} to {', '.join(status['recipients'])}**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
