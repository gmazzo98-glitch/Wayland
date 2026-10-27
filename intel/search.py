"""Polite, pluggable public-web search for deep-intelligence collectors.

Providers return snippets, URLs and dates only; page content is fetched by ``PoliteFetcher``
so robots, rate limits and evidence grounding remain in one place.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
import threading
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote_plus, urlparse

import requests

BLOCKED_DOMAINS = {"facebook.com", "instagram.com", "linkedin.com", "paginegialle.it", "kompass.com"}


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    date: str = ""
    provider: str = ""
    query: str = ""


class SearchResponse(list):
    """List-compatible results with a health record for each attempted provider."""

    def __init__(self, results=(), provider_health=None):
        super().__init__(results)
        self.provider_health = provider_health or {}


class ProviderError(Exception):
    pass


def _check_response(response):
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise ProviderError(f"HTTP {status}")
    return response


def canonical_url(url: str) -> str:
    url = (url or "").split("#", 1)[0].strip()
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower() or 'https'}://{host}{path}"


def _blocked(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in BLOCKED_DOMAINS)


class SearchClient:
    def __init__(self, fetcher=None, session: Optional[requests.Session] = None,
                 cache_dir: Optional[str] = None, timeout: int = 20, delay: float = 1.0,
                 providers: Optional[Iterable[str]] = None) -> None:
        self.fetcher = fetcher
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update({"User-Agent": "ViennaResearchBot/1.0 (+https://wayland.my)"})
        self.cache_dir, self.timeout, self.delay = cache_dir, timeout, delay
        self.providers = tuple(("duckduckgo", "google-news", "gdelt", "sec") if providers is None else providers)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def search(self, query: str, kind: str = "web", lang: str = "it", n: int = 10) -> List[SearchResult]:
        if not query.strip() or n <= 0:
            return []
        cache_key = hashlib.sha256(json.dumps([query, kind, lang, n, self.providers]).encode()).hexdigest()
        if self.cache_dir:
            path = os.path.join(self.cache_dir, cache_key + ".json")
            try:
                if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
                    with open(path, encoding="utf-8") as fh:
                        cached = json.load(fh)
                    return SearchResponse([SearchResult(**x) for x in cached], {"cache": "ok"})
            except (OSError, ValueError, TypeError):
                pass
        results: List[SearchResult] = []
        health = {}
        for provider in self.providers:
            if len(results) >= n:
                break
            try:
                items = self._provider(provider, query, kind, lang, n - len(results))
                results.extend(items)
                health[provider] = "ok"
            except (requests.RequestException, ValueError, ET.ParseError, ProviderError) as exc:
                # Do not include request URLs: keyed provider URLs can contain secrets.
                health[provider] = f"error: {type(exc).__name__}" + (f" ({exc})" if isinstance(exc, ProviderError) else "")
        unique = []
        seen = set()
        for item in results:
            item.query = query
            key = canonical_url(item.url)
            if key and key not in seen and not _blocked(key):
                seen.add(key)
                unique.append(item)
        unique = unique[:n]
        if self.cache_dir and not any(value.startswith("error:") for value in health.values()):
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(os.path.join(self.cache_dir, cache_key + ".json"), "w", encoding="utf-8") as fh:
                    json.dump([asdict(x) for x in unique], fh)
            except OSError:
                pass
        return SearchResponse(unique, health)

    def _provider(self, name: str, query: str, kind: str, lang: str, n: int) -> List[SearchResult]:
        if name == "duckduckgo":
            return self._duckduckgo(query, n)
        if name == "google-news":
            return self._google_news(query, lang, n)
        if name == "gdelt":
            return self._gdelt(query, n)
        if name == "sec":
            return self._sec(query, n)
        if name in {"brave", "serper", "tavily", "serpapi"}:
            return self._keyed(name, query, n)
        raise ProviderError("unknown provider")

    def _get(self, url: str, host_key: str = "search"):
        with self._lock:
            wait = self.delay - (time.monotonic() - self._last.get(host_key, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[host_key] = time.monotonic()
        return _check_response(self.session.get(url, timeout=self.timeout))

    def _duckduckgo(self, query, n):
        from bs4 import BeautifulSoup
        resp = self._get("https://html.duckduckgo.com/html/?q=" + quote_plus(query), "duckduckgo")
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        for node in soup.select(".result")[:n]:
            link = node.select_one("a.result__a")
            if link and link.get("href"):
                snippet_node = node.select_one(".result__snippet")
                out.append(SearchResult(link["href"], link.get_text(" ", strip=True),
                                        snippet_node.get_text(" ", strip=True) if snippet_node else "", provider="duckduckgo"))
        return out

    def _google_news(self, query, lang, n):
        url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=" + quote_plus(lang)
        root = ET.fromstring(self._get(url, "news.google.com").content)
        return [SearchResult(i.findtext("link", ""), i.findtext("title", ""), i.findtext("description", ""),
                             i.findtext("pubDate", ""), "google-news") for i in root.findall(".//item")[:n]]

    def _gdelt(self, query, n):
        root = self._get("https://api.gdeltproject.org/api/v2/doc/doc?query=" + quote_plus(query) + "&mode=artlist&format=json&maxrecords=" + str(n), "api.gdeltproject.org").json()
        return [SearchResult(x.get("url", ""), x.get("title", ""), x.get("snippet", ""), x.get("seendate", ""), "gdelt") for x in root.get("articles", [])]

    def _sec(self, query, n):
        data = self._get("https://efts.sec.gov/LATEST/search-index?q=" + quote_plus(query) + "&from=0&size=" + str(n), "efts.sec.gov").json()
        out = []
        for hit in data.get("hits", {}).get("hits", [])[:n]:
            src = hit.get("_source", {})
            adsh = src.get("adsh", "")
            ciks = src.get("ciks", [])
            cik = str(ciks[0]).lstrip("0") if ciks else ""
            adsh_clean = adsh.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_clean}/{adsh}.txt" if (cik and adsh) else ""
            title = ", ".join(src.get("display_names", []))
            form = src.get("form", "")
            desc = src.get("file_description") or form
            date = src.get("file_date", "")
            if url:
                out.append(SearchResult(url=url, title=f"{title} ({form})", snippet=desc, date=date, provider="sec"))
        return out

    def _keyed(self, provider, query, n):
        env = {"brave": "BRAVE_SEARCH_API_KEY", "serper": "SERPER_API_KEY", "tavily": "TAVILY_API_KEY", "serpapi": "SERPAPI_API_KEY"}[provider]
        key = os.getenv(env)
        if not key:
            raise ProviderError(f"missing {env}")
        try:
            if provider == "serper":
                resp = self.session.post("https://google.serper.dev/search",
                                         headers={"X-API-KEY": key, "Content-Type": "application/json"},
                                         json={"q": query, "num": n}, timeout=self.timeout)
                data = _check_response(resp).json()
                return [SearchResult(x.get("link", ""), x.get("title", ""), x.get("snippet", ""), provider="serper")
                        for x in data.get("organic", [])[:n]]
            if provider == "brave":
                resp = self.session.get("https://api.search.brave.com/res/v1/web/search",
                                        headers={"X-Subscription-Token": key, "Accept": "application/json"},
                                        params={"q": query, "count": n}, timeout=self.timeout)
                data = _check_response(resp).json()
                return [SearchResult(x.get("url", ""), x.get("title", ""), x.get("description", ""), provider="brave")
                        for x in data.get("web", {}).get("results", [])[:n]]
            if provider == "tavily":
                resp = self.session.post("https://api.tavily.com/search",
                                         json={"api_key": key, "query": query, "max_results": n}, timeout=self.timeout)
                data = _check_response(resp).json()
                return [SearchResult(x.get("url", ""), x.get("title", ""), x.get("content", ""), provider="tavily")
                        for x in data.get("results", [])[:n]]
        except (requests.RequestException, ValueError, ProviderError):
            raise
        raise ProviderError("provider not implemented")
