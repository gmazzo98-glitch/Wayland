"""
A deliberately small, polite page fetcher: the default `ctx.fetch` for collectors and for scripts/intel_run.py.

Ticket I05 owns hardening it (caching, a browser fallback for JavaScript-only sites, per-domain limits shared
across worker PCs). Until then it gives every collector the same honest behaviour: identify ourselves, honour
robots.txt, wait between requests to one host, never follow a page we are not allowed to read, and return TEXT
(HTML reduced to readable text, PDFs read with pypdf) plus the response headers (which carry facts too:
`Server`, `X-Powered-By`).

Optional libraries (bs4, pypdf) are imported lazily; without them HTML is reduced with a regex and PDFs are not
read, and the result says so in `Page.note`. Nothing here raises for an ordinary failure: a blocked, missing or
unreadable page comes back as a Page with `ok=False` and the reason, so a collector can record an honest absence.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; ViennaResearchBot/1.0; +https://wayland.my)"
MAX_BYTES = 6_000_000
DEFAULT_DELAY = 1.0


@dataclass
class Page:
    url: str
    ok: bool
    status: Optional[int] = None
    content_type: str = ""
    title: str = ""
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)      # absolute, same-document links in page order
    note: str = ""                                       # why it is not ok, or a caveat about the text
    content_hash: str = ""


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    return urldefrag(url)[0]


def html_to_text(html: str):
    """(title, text, links-as-hrefs) from HTML. bs4 when available, a regex otherwise."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml") if _has("lxml") else BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        hrefs = [a["href"] for a in soup.find_all("a", href=True)]
        for tag in soup(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
    except ImportError:
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return title, text, hrefs


def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:40])


class PoliteFetcher:
    def __init__(self, delay: float = DEFAULT_DELAY, timeout: int = 20, respect_robots: bool = True,
                 session: Optional[requests.Session] = None, cache_dir: Optional[str] = None,
                 cache_ttl: int = 86400, browser_fallback: bool = False) -> None:
        self.delay, self.timeout, self.respect_robots = delay, timeout, respect_robots
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it,en;q=0.8,de;q=0.6"})
        self._robots: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.pages_fetched = 0
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        self.browser_fallback = browser_fallback

    def _cache_path(self, url: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str) -> Optional[dict]:
        path = self._cache_path(url)
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                item = json.load(fh)
            if self.cache_ttl >= 0 and time.time() - item.get("saved", 0) > self.cache_ttl:
                return None
            return item
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, url: str, page: Page) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"saved": time.time(), "page": page.__dict__}, fh)
        except OSError:
            pass

    @staticmethod
    def _page_from_cache(item: dict) -> Optional[Page]:
        try:
            return Page(**item["page"])
        except (KeyError, TypeError):
            return None

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urlparse(url).netloc
        if host not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.session.get(f"{urlparse(url).scheme}://{host}/robots.txt", timeout=self.timeout)
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rp = None   # unreachable robots.txt: treat as no restrictions, as crawlers conventionally do
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _wait(self, host: str) -> None:
        with self._lock:
            gap = self.delay - (time.monotonic() - self._last.get(host, 0.0))
            self._last[host] = time.monotonic() + max(gap, 0.0)
        if gap > 0:
            time.sleep(gap)

    def __call__(self, url: str) -> Page:
        return self.fetch(url)

    def fetch(self, url: str) -> Page:
        url = normalise_url(url)
        if not url:
            return Page(url="", ok=False, note="empty url")
        cached = self._read_cache(url)
        cached_page = self._page_from_cache(cached) if cached else None
        if cached_page is not None:
            return cached_page
        if not self._allowed(url):
            return Page(url=url, ok=False, note="blocked by robots.txt")
        self._wait(urlparse(url).netloc)
        try:
            request_headers = {}
            if cached and cached.get("etag"):
                request_headers["If-None-Match"] = cached["etag"]
            if cached and cached.get("last_modified"):
                request_headers["If-Modified-Since"] = cached["last_modified"]
            resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True,
                                    headers=request_headers)
            if resp.status_code == 304 and cached_page:
                return cached_page
            raw = resp.raw.read(MAX_BYTES + 1, decode_content=True)
        except requests.RequestException as exc:
            return Page(url=url, ok=False, note=f"{type(exc).__name__}: {str(exc)[:160]}")
        self.pages_fetched += 1
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        headers = {k: v for k, v in resp.headers.items()}
        page = Page(url=resp.url, ok=resp.status_code < 400, status=resp.status_code, content_type=ctype, headers=headers,
                    content_hash=hashlib.sha1(raw[:MAX_BYTES]).hexdigest())
        if not page.ok:
            page.note = f"HTTP {resp.status_code}"
            return page
        if len(raw) > MAX_BYTES:
            page.note = "truncated at 6 MB"
        if ctype == "application/pdf" or resp.url.lower().endswith(".pdf"):
            try:
                page.text = pdf_to_text(raw[:MAX_BYTES])
            except Exception as exc:   # pypdf missing, encrypted or scanned PDF: say so, do not fail the run
                page.ok, page.note = False, f"pdf not readable: {type(exc).__name__}"
            return page
        if "html" in ctype or ctype in ("", "text/plain"):
            encoding = resp.encoding or (resp.apparent_encoding if hasattr(resp, "apparent_encoding") else None) or "utf-8"
            html = raw[:MAX_BYTES].decode(encoding, errors="replace")
            page.title, page.text, hrefs = html_to_text(html)
            if self.browser_fallback and len(page.text.strip()) < 80:
                rendered = self._render_with_browser(resp.url)
                if rendered:
                    page.title, page.text, hrefs = html_to_text(rendered)
                    page.note = "rendered with optional browser fallback"
            seen, links = set(), []
            for h in hrefs:
                absolute = urldefrag(urljoin(resp.url, h.strip()))[0]
                if absolute.startswith("http") and absolute not in seen:
                    seen.add(absolute)
                    links.append(absolute)
            page.links = links
        self._write_cache(url, page)
        return page

    def _render_with_browser(self, url: str) -> str:
        """Best-effort Playwright fallback; it is optional and never makes fetching fail."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    return page.content()
                finally:
                    browser.close()
        except Exception:
            return ""
