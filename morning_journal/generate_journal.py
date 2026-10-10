#!/usr/bin/env python3
"""Wayland Morning Journal — daily generator.

Fetches the latest commits of the Wayland repository and European/Italian
startup & SME news feeds, has a Mistral model write the daily report, and
emails it to the configured recipients.

Sending uses the official Gmail API with OAuth (same approach as the
Email-Automatizer repo) — no Google App Password required. One-time setup
with gmail_oauth_setup.py produces a refresh token; see README.md.

Environment variables (GitHub Actions secrets):
  MISTRAL_API_KEY        - Mistral API key (console.mistral.ai)
  GMAIL_CLIENT_ID        - Google Cloud OAuth client ID
  GMAIL_CLIENT_SECRET    - Google Cloud OAuth client secret
  GMAIL_REFRESH_TOKEN    - refresh token from gmail_oauth_setup.py

Optional:
  GMAIL_APP_PASSWORD     - legacy SMTP fallback (if you ever get one)
  GMAIL_USER            - sending address (default: gmazzo98@gmail.com)
  JOURNAL_RECIPIENTS    - comma-separated (default: gmazzo98@gmail.com,giovannigatti.ita@gmail.com)
  JOURNAL_REPO          - repo full name (default: gmazzo98-glitch/Wayland)
  JOURNAL_MODEL         - Mistral model (default: mistral-small-latest)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

RSS_FEEDS = [
    ("EU-Startups", "https://www.eu-startups.com/feed/"),
    ("Tech.eu", "https://tech.eu/feed/"),
    ("Startupbusiness (IT)", "https://www.startupbusiness.it/feed/"),
]


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wayland-morning-journal/1.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_commits(repo_full_name: str, per_page: int = 30) -> list[dict]:
    url = f"https://api.github.com/repos/{repo_full_name}/commits?per_page={per_page}"
    data = json.loads(http_get(url, headers={"Accept": "application/vnd.github+json"}))
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
        html = re.sub(r"^```(html)?s*|s*```$", "", html, flags=re.S).strip()
    return {"html": html, "subject": subject}


def build_mime(report: dict, recipients: list[str], sender: str) -> MIMEMultipart:
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
    return msg


def gmail_api_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(GMAIL_TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def send_via_gmail_api(msg: MIMEMultipart, client_id: str, client_secret: str, refresh_token: str) -> str:
    token = gmail_api_access_token(client_id, client_secret, refresh_token)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body = json.dumps({"raw": raw}).encode("utf-8")
    req = urllib.request.Request(GMAIL_SEND_URL, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp.get("id", "")


def send_via_smtp(msg: MIMEMultipart, user: str, password: str, recipients: list[str]) -> None:
    import smtplib
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())


def send_email(report: dict, recipients: list[str], sender: str, env: dict) -> dict:
    msg = build_mime(report, recipients, sender)
    if env.get("client_id") and env.get("client_secret") and env.get("refresh_token"):
        message_id = send_via_gmail_api(msg, env["client_id"], env["client_secret"], env["refresh_token"])
        return {"sent": True, "via": "gmail-api", "message_id": message_id,
                "subject": report["subject"], "recipients": recipients}
    if env.get("app_password"):
        send_via_smtp(msg, sender, env["app_password"], recipients)
        return {"sent": True, "via": "smtp", "subject": report["subject"], "recipients": recipients}
    return {"sent": False, "reason": "No Gmail credentials configured. Add GMAIL_CLIENT_ID, "
            "GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN (see morning_journal/README.md)."}


def main() -> int:
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    sender = os.environ.get("GMAIL_USER", "gmazzo98@gmail.com").strip()
    recipients = [r.strip() for r in os.environ.get(
        "JOURNAL_RECIPIENTS", "gmazzo98@gmail.com,giovannigatti.ita@gmail.com").split(",") if r.strip()]
    repo = os.environ.get("JOURNAL_REPO", "gmazzo98-glitch/Wayland").strip()
    model = os.environ.get("JOURNAL_MODEL", "mistral-small-latest").strip()

    env = {
        "client_id": os.environ.get("GMAIL_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GMAIL_CLIENT_SECRET", "").strip(),
        "refresh_token": os.environ.get("GMAIL_REFRESH_TOKEN", "").strip(),
        "app_password": os.environ.get("GMAIL_APP_PASSWORD", "").strip(),
    }

    if not api_key:
        print("ERROR: missing required secret MISTRAL_API_KEY. "
              "Add it under Settings > Secrets and variables > Actions.")
        return 1
    if not ((env["client_id"] and env["client_secret"] and env["refresh_token"]) or env["app_password"]):
        print("ERROR: no Gmail credentials. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and "
              "GMAIL_REFRESH_TOKEN (one-time setup with morning_journal/gmail_oauth_setup.py).")
        return 1

    commits = fetch_commits(repo)
    news = fetch_news()
    print(f"Fetched {len(commits)} commits and {len(news)} news items.")
    report = write_report(commits, news, model, recipients, api_key)
    status = send_email(report, recipients, sender, env)
    print(f"Email status: {status}")
    return 0 if status.get("sent") else 1


if __name__ == "__main__":
    sys.exit(main())
