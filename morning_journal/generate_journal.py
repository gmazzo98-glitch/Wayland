#!/usr/bin/env python3
"""Wayland Morning Journal — daily generator (Google Drive edition).

Fetches the latest commits of the Wayland repository and European/Italian
startup & SME news feeds, has a Mistral model write the daily report, and
saves it as a browsable HTML page into the shared Google Drive folder
"Wayland Morning Journal" through a Google Apps Script web-app bridge.

Environment variables (GitHub Actions secrets):
  MISTRAL_API_KEY     - Mistral API key (console.mistral.ai)
  JOURNAL_BRIDGE_URL  - deployed Apps Script web app URL

Optional:
  JOURNAL_REPO        - repo full name (default: gmazzo98-glitch/Wayland)
  JOURNAL_MODEL       - Mistral model (default: mistral-small-latest)
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

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
    req = urllib.request.Request(url, headers={"User-Agent": "wayland-morning-journal/2.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "wayland-morning-journal/2.0",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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


def write_report(commits: list[dict], news: list[dict], model: str, api_key: str) -> dict:
    now_rome = datetime.now(ROME)
    subject = f"The Wayland Morning Journal {now_rome:%d.%m.%y}"

    commits_text = "\n\n".join(
        f"- [{c['date']}] {c['sha']} by {c['author']}: {c['message']}" for c in commits
    ) or "(no commits found)"
    news_text = "\n\n".join(
        f"- [{n['date']}] ({n['source']}) {n['title']} — {n['description']} Link: {n['link']}" for n in news
    ) or "(no news items found)"

    prompt = f"""Today is {now_rome:%d %B %Y}. You are writing "The Wayland Morning Journal", a daily report about the Wayland repository (Project Vienna: a Streamlit company-screening dashboard that sources public signals, scores them into indicators, and surfaces pain points and valuations, focused on Italian SMEs) and about the Italian and European startup & SME ecosystem.

The report title is exactly: "{subject}"

Using the data below, write the report in English, structured in three sections:

1) REPOSITORY UPDATES — was the code updated? Explain the new main functionalities in plain language, each as a short list entry with: what it does, why it matters, a short tutorial/how-to-use note, and its capabilities. If no commits are new, say so briefly and summarize the most recent ones.

2) SCOPE ANALYSIS & ECOSYSTEM DIGEST — first silently analyse the scope of the Wayland tool and which startup categories could provide innovative solutions to SMEs matched by this data layer (GovTech/B2G intelligence, vertical AI/SaaS for SME digitization, M&A/succession advisory, SME credit-scoring fintech, etc.); then a digest of the news and pains of the Italian and European startup and SME ecosystem based on the news items provided. Mention concrete facts, numbers and dates when present in the data.

3) POINTS OF VIEW & OUTLOOKS — interesting perspectives on the situation and possible outlooks for the Wayland tool and project, including risks and next steps.

Style: clear, direct, business-savvy, no fluff, no invented facts — use only the data provided, and clearly mark anything uncertain.

GITHUB COMMITS (newest first):
{commits_text}

NEWS ITEMS:
{news_text}

The report will be saved to the shared "Wayland Morning Journal" Google Drive folder, where stakeholders read it every morning.
Output ONLY the HTML for the report body: a single <div> container (no <html>/<head>/<body>), using <h2> for the three section titles, <h3> and <ul>/<li> for entries, <strong> for emphasis. No inline CSS styles, no <script>, no images."""

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


def build_html_document(report: dict) -> str:
    title = escape(report["subject"])
    style = ("body{font-family:Arial,Helvetica,sans-serif;max-width:760px;"
             "margin:24px auto;padding:0 16px;color:#1a1a1a;line-height:1.5}"
             "h1{border-bottom:3px solid #2b6cb0;padding-bottom:8px}"
             "h2{color:#2b6cb0}a{color:#2b6cb0}"
             ".meta{color:#666;font-size:0.9em}")
    return ("<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{title}</title>\n<style>{style}</style>\n</head>\n<body>\n"
            f"<h1>{title}</h1>\n<p class=\"meta\">Generated {datetime.now(ROME):%d.%m.%Y %H:%M} Europe/Rome</p>\n"
            f"{report['html']}\n</body>\n</html>\n")


def upload_to_drive(bridge_url: str, report: dict, document: str) -> dict:
    filename = report["subject"] + ".html"
    resp = http_post_json(bridge_url, {
        "subject": report["subject"],
        "filename": filename,
        "html": document,
    })
    if not resp.get("ok"):
        raise RuntimeError(f"Drive bridge rejected the report: {resp.get('error')}")
    return {"filename": filename, "detail": resp}


def main() -> int:
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    bridge_url = os.environ.get("JOURNAL_BRIDGE_URL", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("JOURNAL_REPO", "gmazzo98-glitch/Wayland").strip()
    model = os.environ.get("JOURNAL_MODEL", "mistral-small-latest").strip()

    summary("- MISTRAL_API_KEY: " + ("present" if api_key else "MISSING"))
    summary("- JOURNAL_BRIDGE_URL: " + ("present" if bridge_url else "MISSING"))

    missing = [name for name, val in (("MISTRAL_API_KEY", api_key),
                                       ("JOURNAL_BRIDGE_URL", bridge_url)) if not val]
    if missing:
        print(f"ERROR: missing required secrets: {', '.join(missing)}.")
        print("::error::Missing secrets: " + ", ".join(missing) + ". Add them under Settings > Secrets and variables > Actions.")
        summary("**FAILED: missing secrets: " + ", ".join(missing) + "**")
        return 1

    try:
        commits = fetch_commits(repo, token=github_token)
        news = fetch_news()
        print(f"Fetched {len(commits)} commits and {len(news)} news items.")
        summary(f"- Fetched {len(commits)} commits and {len(news)} news items")
        report = write_report(commits, news, model, api_key)
        document = build_html_document(report)
        saved = upload_to_drive(bridge_url, report, document)
    except Exception:
        tb_last = traceback.format_exc().strip().splitlines()[-1]
        print("::error::" + tb_last[:250])
        summary("FAILED with unhandled error:\n\n" + "```" + "\n" + traceback.format_exc() + "\n" + "```")
        raise
    print(f"Drive status: {saved['filename']} -> {saved['detail']}")
    print("::notice::Journal saved to Drive: " + saved["filename"])
    summary(f"- **Saved to Drive**: {saved['filename']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
