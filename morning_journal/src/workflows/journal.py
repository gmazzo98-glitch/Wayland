"""Wayland Morning Journal — daily repo analysis + ecosystem digest, emailed automatically."""
from __future__ import annotations

import json
import os
import re
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import List
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pydantic import BaseModel

try:
    from mistralai.workflows import activity, workflow
except ImportError:  # fallback for alternate SDK layouts
    from mistralai import workflows as _wf

    activity = _wf.activity
    workflow = _wf.workflow

from mistralai.workflows.client import get_mistral_client

RSS_FEEDS = [
    ("EU-Startups", "https://www.eu-startups.com/feed/"),
    ("Tech.eu", "https://tech.eu/feed/"),
    ("Startupbusiness (IT)", "https://www.startupbusiness.it/feed/"),
]

ROME = ZoneInfo("Europe/Rome")


def _http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wayland-morning-journal/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"s+", " ", text).strip()


@activity()
async def fetch_commits(repo_full_name: str, per_page: int = 30) -> List[dict]:
    """Fetch the most recent commits of the repo from the GitHub API."""
    url = f"https://api.github.com/repos/{repo_full_name}/commits?per_page={per_page}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.loads(_http_get(url, headers=headers).decode("utf-8"))
    commits = []
    for c in data:
        commits.append(
            {
                "sha": c["sha"][:8],
                "date": c["commit"]["author"]["date"],
                "author": c["commit"]["author"]["name"],
                "message": c["commit"]["message"][:1500],
                "url": c["html_url"],
            }
        )
    return commits


@activity()
async def fetch_news(max_days_back: int = 5, max_items_per_feed: int = 12) -> List[dict]:
    """Fetch recent items from European and Italian startup/SME news RSS feeds."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days_back)
    items: List[dict] = []
    for source, feed_url in RSS_FEEDS:
        try:
            raw = _http_get(feed_url)
            root = ElementTree.fromstring(raw)
        except Exception as exc:  # a broken feed must never kill the journal
            items.append({"source": source, "title": f"[feed unavailable: {exc.__class__.__name__}]", "description": "", "link": feed_url, "date": ""})
            continue
        count = 0
        for item in root.iter("item"):
            if count >= max_items_per_feed:
                break
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = _strip_html(item.findtext("description") or "")[:400]
            try:
                when = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
                if when < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            items.append({"source": source, "title": title, "description": desc, "link": link, "date": pub})
            count += 1
    return items


@activity()
async def write_report(commits: List[dict], news: List[dict], model: str, recipients: List[str]) -> dict:
    """Have a Mistral model write the journal report and return {html, subject}."""
    client = get_mistral_client()
    now_rome = datetime.now(ROME)
    subject = f"The Wayland Morning Journal {now_rome:%d.%m.%y}"

    commits_text = "

".join(
        f"- [{c['date']}] {c['sha']} by {c['author']}: {c['message']}" for c in commits
    ) or "(no commits found)"
    news_text = "

".join(
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

    response = await client.chat.complete_async(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )
    html = (response.choices[0].message.content or "").strip()
    if html.startswith("```"):
        html = re.sub(r"^```(html)?s*|s*```$", "", html, flags=re.S).strip()
    return {"html": html, "subject": subject}


@activity()
async def send_email(report: dict, recipients: List[str]) -> dict:
    """Send the report via Gmail SMTP using workspace-secrets-bound credentials."""
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not password:
        return {"sent": False, "reason": "GMAIL_USER / GMAIL_APP_PASSWORD not configured; report generated but not sent."}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = report["subject"]
    msg["From"] = f"Wayland Journal <{user}>"
    msg["To"] = ", ".join(recipients)
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;color:#1a1a1a;line-height:1.5">'
        f'<h1 style="border-bottom:3px solid #2b6cb0;padding-bottom:8px">{escape(report["subject"])}</h1>'
        f"{report['html']}"
        "</div>"
    )
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", html), "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
            server.login(user, password)
            server.sendmail(user, recipients, msg.as_string())
    except Exception as exc:
        return {"sent": False, "reason": f"SMTP error: {exc.__class__.__name__}: {exc}"}
    return {"sent": True, "subject": report["subject"], "recipients": recipients}


class JournalParams(BaseModel):
    recipients: List[str] = ["gmazzo98@gmail.com", "giovannigatti.ita@gmail.com"]
    repo_full_name: str = "gmazzo98-glitch/Wayland"
    model: str = "mistral-medium-latest"


@workflow.define(
    name="wayland-morning-journal",
    workflow_display_name="Wayland Morning Journal",
    workflow_description="Analyses the Wayland repo commits and European/Italian startup-SME news, then writes and emails the daily Wayland Morning Journal.",
)
class WaylandMorningJournal:
    @workflow.entrypoint
    async def run(self, params: JournalParams) -> dict:
        commits = await fetch_commits(params.repo_full_name)
        news = await fetch_news()
        report = await write_report(commits, news, params.model, params.recipients)
        status = await send_email(report, params.recipients)
        return {
            "commits_analyzed": len(commits),
            "news_items": len(news),
            "subject": report["subject"],
            "email": status,
        }
