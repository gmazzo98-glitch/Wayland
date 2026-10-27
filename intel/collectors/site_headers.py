"""
Collector `site-headers`: what the company's own web server says about itself, and how its transport is secured.

Written first as the worked example every other collector copies (ticket I02), and it fixes a real false negative:
scrapers/wappalyzer_local.py only knows 18 HTML signatures (WordPress, Shopify, ...), so ASCO Filtri's site, which
answers `Server: Microsoft-IIS/10.0` and `X-Powered-By: ASP.NET`, was recorded as "no technology detected".
Ticket I18 extended it: CDN and hosting from headers, framework from session cookies, end-of-life server software,
the security-header set, and the TLS certificate. The markup side (scripts, meta tags, site quality) is in
`intel/collectors/tech_stack.py`; both write under `digital.` and share the helpers defined here (`FactSink`,
`fetch_homepage`). A key may be produced by both files (same fact, different evidence merges in the ledger).

What to copy from here:
  * all page access goes through ctx.fetch (polite, robots-aware), never `requests` directly;
  * a fact's quote is text that really occurs in what was fetched, checked with quote_in_text() before the Evidence
    is built; a fact that cannot be quoted is not emitted;
  * every fact passes the verification gate (intel/verify.py) before it is returned: grounding, entity match and
    collision checks; a "rejected" verdict is dropped and reported, an "unverified" one is kept as unverified;
  * the collector states what it CHECKED even when it found nothing (`checked_absent`);
  * it emits only keys under its declared `produces` prefixes and writes nothing to the database.

Not a score and not a signal: `tech_stack_intensity` is untouched (ticket I35 owns score-moving changes).
"""

from __future__ import annotations

import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.fetch import Page, normalise_url
from intel.ledger import Evidence, Fact, quote_in_text
from intel.verify import verify

COLLECTOR_TAG = "I18:site-headers@2"

# header name (lower-case) -> fact key. The value is the header value as the server sent it.
HEADER_FACTS = {
    "server": "digital.web_server",
    "x-powered-by": "digital.web_framework",
    "x-aspnet-version": "digital.web_framework",
    "x-aspnetmvc-version": "digital.web_framework",
    "x-generator": "digital.cms",
    "x-powered-by-plesk": "digital.hosting_panel",
    "x-drupal-cache": "digital.cms",
    "x-shopify-stage": "digital.ecommerce_platform",
}

# Session-cookie names that give the application framework away (the cookie NAME is the evidence, never the value).
COOKIE_FRAMEWORKS = (
    (r"\bPHPSESSID=", "PHP"),
    (r"\bASP\.NET_SessionId=", "ASP.NET"),
    (r"\.AspNetCore\.[A-Za-z]+=", "ASP.NET Core"),
    (r"\bJSESSIONID=", "Java servlet container"),
    (r"\blaravel_session=", "Laravel (PHP)"),
    (r"\bci_session=", "CodeIgniter (PHP)"),
    (r"\bcsrftoken=", "Django (Python)"),
    (r"\b_rails_session=|\b_session_id=", "Ruby on Rails"),
)

# (CDN or hosting name, header-name regex, value regex or None). Matched on lower-cased header names.
CDN_RULES = (
    ("Cloudflare", r"^cf-ray$", None), ("Cloudflare", r"^server$", r"^cloudflare"),
    ("Amazon CloudFront", r"^x-amz-cf-id$", None), ("Amazon CloudFront", r"^via$", r"cloudfront"),
    ("Akamai", r"^x-akamai-", None), ("Akamai", r"^server$", r"akamaighost|akamainetstorage"),
    ("Fastly", r"^x-fastly-request-id$", None), ("Fastly", r"^x-served-by$", r"^cache-"),
    ("Azure Front Door", r"^x-azure-ref$", None),
    ("Vercel", r"^x-vercel-id$", None), ("Vercel", r"^server$", r"^vercel"),
    ("Netlify", r"^x-nf-request-id$", None), ("Netlify", r"^server$", r"^netlify"),
    ("Imperva", r"^x-iinfo$", None), ("Imperva", r"^x-cdn$", r"imperva|incapsula"),
    ("Sucuri", r"^x-sucuri-id$", None),
    ("GitHub Pages", r"^server$", r"^github\.com$"),
)

# The six response headers an auditor looks for first. (label, header name)
SECURITY_HEADERS = (
    ("HSTS", "strict-transport-security"), ("CSP", "content-security-policy"),
    ("X-Frame-Options", "x-frame-options"), ("X-Content-Type-Options", "x-content-type-options"),
    ("Referrer-Policy", "referrer-policy"), ("Permissions-Policy", "permissions-policy"),
)

# End-of-support dates of server software, from the vendors' lifecycle pages. REVIEW YEARLY (reviewed 2026-10-27).
#   PHP: https://www.php.net/supported-versions.php   Apache httpd 2.2: ended 2017-12-31 (httpd.apache.org)
#   IIS: follows the Windows Server lifecycle (learn.microsoft.com/lifecycle)   OpenSSL: openssl.org/policies/releasestrat
# A Linux distribution may still back-port fixes to an "ended" upstream branch, so the value says "upstream".
PHP_EOL = {"5.6": "2018-12-31", "7.0": "2018-12-03", "7.1": "2019-12-01", "7.2": "2020-11-30", "7.3": "2021-12-06",
           "7.4": "2022-11-28", "8.0": "2023-11-26", "8.1": "2025-12-31"}
IIS_EOL = {"6.0": "2015-07-14", "7.0": "2020-01-14", "7.5": "2020-01-14", "8.0": "2023-10-10", "8.5": "2023-10-10"}
APACHE_EOL = {"2.0": "2013-07-10", "2.2": "2017-12-31"}
OPENSSL_EOL = {"0.9": "2015-12-31", "1.0.1": "2016-12-31", "1.0.2": "2019-12-31", "1.1.0": "2019-09-11",
               "1.1.1": "2023-09-11"}

TLS_EXPIRY_WARNING_DAYS = 14
TLS_TIMEOUT_SECONDS = 6.0


# ---- shared helpers (also used by tech_stack.py) ------------------------------------------------------------

def header_block(headers: Dict[str, str]) -> str:
    """The response headers as one text, the string every header quote is checked against."""
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def _original_case(headers, lowered_name: str) -> str:
    for k in headers:
        if k.lower() == lowered_name:
            return k
    return lowered_name


def quote_prefix(text: str, limit: int = 390) -> str:
    """A verbatim head of `text`, cut at a line boundary, short enough to be a ledger quote."""
    out = ""
    for line in text.splitlines():
        if len(out) + len(line) + 1 > limit:
            break
        out += ("\n" if out else "") + line
    return out or text[:limit]


class FactSink:
    """Builds ledger Facts for one collector run: checks that the quote really occurs in the text the collector
    fetched, passes the fact through the verification gate (intel/verify.py), and drops what the gate rejects.

    `anchor_text` is the homepage text: it is appended to the text the gate sees, so the gate can answer "does this
    site talk about THIS company?" even for a fact whose own source (a header block, an API answer) names nobody.
    A site that never names the company yields `unverified` facts, which the dossier hides by default."""

    def __init__(self, ctx: CollectorContext, result: CollectorResult, tag: str, anchor_text: str = "") -> None:
        self.ctx, self.result, self.tag, self.anchor_text = ctx, result, tag, anchor_text
        self._seen: set = set()
        self.dropped = 0

    def add(self, key: str, value: Any, *, quote: str, source_text: str, url: str, grade: str = "B",
            confidence: float = 0.9, source_type: str = "company_site", title: Optional[str] = None,
            as_of: Optional[str] = None, notes: Optional[str] = None, content_hash: Optional[str] = None,
            published_at: Optional[str] = None) -> Optional[Fact]:
        quote = re.sub(r"\s+", " ", quote or "").strip()
        if not quote_in_text(quote, source_text):
            self.dropped += 1
            self.result.errors.append(f"{key}: quote not found in the fetched text, fact dropped ({quote[:50]!r})")
            return None
        fact = Fact(key=key, value=value, grade=grade, confidence=confidence, collector=self.tag, as_of=as_of,
                    notes=notes, evidence=[Evidence(source_type=source_type, url=url, quote=quote[:400], title=title,
                                                    content_hash=content_hash, published_at=published_at)])
        if fact.id in self._seen:
            return None
        verdict = verify(fact, source_text + "\n" + self.anchor_text, self.ctx.company, self.ctx.ledger)
        if verdict.decision == "rejected":
            self.dropped += 1
            self.result.errors.append(f"{key}: rejected by the verification gate: {'; '.join(verdict.reasons)[:160]}")
            return None
        if notes and not fact.notes:
            fact.notes = notes
        self._seen.add(fact.id)
        self.result.facts.append(fact)
        return fact


def fetch_homepage(ctx: CollectorContext, result: CollectorResult) -> Tuple[Optional[Page], str]:
    """Fetch the company homepage through ctx.fetch. When https fails for a transport reason (bad certificate,
    refused connection) the plain-http address is tried once, so the rest of the footprint can still be read.
    Returns (page or None, the url that was asked first); the reasons go into result.errors, never raise."""
    url = normalise_url(ctx.company.get("website_url") or "")
    if not url:
        result.errors.append("company has no website_url")
        return None, ""
    if ctx.fetch is None:
        result.errors.append("no fetcher supplied")
        return None, url
    page = ctx.fetch(url)
    result.pages_fetched += 1
    if (page is None or not page.ok) and url.startswith("https://") and "robots" not in (getattr(page, "note", "") or ""):
        first_note = getattr(page, "note", "") or "unreachable"
        alt = ctx.fetch("http://" + url[len("https://"):])
        result.pages_fetched += 1
        if alt is not None and alt.ok:
            alt.note = alt.note or f"served over plain http only; https failed: {first_note}"
            return alt, url
        return page if page is not None else alt, url
    return page, url


def registrable_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


# ---- TLS ----------------------------------------------------------------------------------------------------

def tls_probe(host: str, port: int = 443, timeout: float = TLS_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """One TLS handshake to the company's own server, validated with the system trust store.
    Returns {'ok': True, 'version', 'issuer', 'not_after'(ISO date)} or {'ok': False, 'error'} (never raises)."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                not_after = ssl.cert_time_to_seconds(cert["notAfter"]) if cert.get("notAfter") else None
                return {"ok": True, "version": tls.version(),
                        "issuer": issuer.get("organizationName") or issuer.get("commonName") or "unknown issuer",
                        "not_after": datetime.fromtimestamp(not_after, timezone.utc).strftime("%Y-%m-%d")
                        if not_after else None}
    except ssl.SSLCertVerificationError as exc:
        return {"ok": False, "error": f"certificate verification failed: {exc.verify_message or exc.reason or exc}"}
    except ssl.SSLError as exc:
        return {"ok": False, "error": f"TLS handshake failed: {exc.reason or exc}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"connection failed: {type(exc).__name__}", "unreachable": True}


# ---- analysis of a header set -------------------------------------------------------------------------------

def _eol_from_table(branch_table: Dict[str, str], version: str, now: datetime) -> Optional[str]:
    """The end-of-support date when `version` belongs to a branch in the table and that date is past."""
    parts = version.split(".")
    for take in (3, 2):
        branch = ".".join(parts[:take])
        if branch in branch_table:
            return branch_table[branch] if branch_table[branch] <= now.strftime("%Y-%m-%d") else None
    return None


def outdated_server_software(headers: Dict[str, str], now: datetime) -> List[Tuple[str, str, str]]:
    """[(header name lower-case, matched software text, 'upstream support ended YYYY-MM-DD')] for versions the
    server announces itself that are past their vendor's end of support."""
    found: List[Tuple[str, str, str]] = []
    for name, value in ((k.lower(), v) for k, v in headers.items()):
        if name not in ("server", "x-powered-by"):
            continue
        for m in re.finditer(r"PHP/(\d+\.\d+(?:\.\d+)?)", value):
            eol = _eol_from_table(PHP_EOL, m.group(1), now)
            if eol or re.match(r"^[0-4]\.", m.group(1)):
                found.append((name, m.group(0), eol or "2014-08-14"))
        for m in re.finditer(r"Apache/(\d+\.\d+(?:\.\d+)?)", value):
            eol = _eol_from_table(APACHE_EOL, m.group(1), now)
            if eol:
                found.append((name, m.group(0), eol))
        for m in re.finditer(r"Microsoft-IIS/(\d+\.\d+)", value):
            eol = _eol_from_table(IIS_EOL, m.group(1), now)
            if eol:
                found.append((name, m.group(0), eol))
        for m in re.finditer(r"OpenSSL/(\d+\.\d+\.\d+|\d+\.\d+)", value):
            eol = _eol_from_table(OPENSSL_EOL, m.group(1), now)
            if eol:
                found.append((name, m.group(0), eol))
    return found


def csp_is_permissive(csp: str) -> bool:
    """A Content-Security-Policy that allows any source for scripts protects nothing."""
    default_src = re.search(r"(?:default-src|script-src)\s+([^;]*)", csp, re.I)
    return bool(default_src and re.search(r"(^|\s)\*(\s|$)", default_src.group(1)) and "unsafe-inline" in csp)


# ---- the collector ------------------------------------------------------------------------------------------

def run(ctx: CollectorContext, now: Optional[datetime] = None) -> CollectorResult:
    result = CollectorResult()
    now = now or datetime.now(timezone.utc)
    page, asked_url = fetch_homepage(ctx, result)
    if page is None:
        return result
    sink = FactSink(ctx, result, COLLECTOR_TAG, anchor_text=page.text or "")

    if not page.headers:
        result.errors.append(f"no response headers from {asked_url}: {page.note or 'unreachable'}")
        _tls_problem_from_fetch_note(sink, page, asked_url)
        return result
    headers = page.headers
    text = header_block(headers)
    lowered = {k.lower(): v for k, v in headers.items()}
    seen_keys = set()

    def emit(key: str, value: str, quote: str, confidence: float = 0.95, notes: Optional[str] = None) -> None:
        if sink.add(key, value, quote=quote, source_text=text, url=page.url, confidence=confidence, notes=notes,
                    content_hash=page.content_hash, title="HTTP response headers"):
            seen_keys.add(key)

    # 1. what the server announces
    for name, key in HEADER_FACTS.items():
        value = lowered.get(name)
        if value:
            emit(key, value.strip(), f"{_original_case(headers, name)}: {value}")

    # 2. application framework from session-cookie names
    cookies = lowered.get("set-cookie", "")
    for pattern, framework in COOKIE_FRAMEWORKS:
        m = re.search(pattern, cookies)
        if m and not (framework == "Ruby on Rails" and not re.search(r"_rails_session|_session_id", cookies)):
            emit("digital.web_framework", framework, f"{_original_case(headers, 'set-cookie')}: {_cookie_excerpt(cookies, m)}",
                 confidence=0.85)

    # 3. CDN / protection layer / hosting platform
    cdns: Dict[str, str] = {}
    for cdn, name_re, value_re in CDN_RULES:
        for k, v in headers.items():
            if re.search(name_re, k.lower()) and (value_re is None or re.search(value_re, v.strip().lower())):
                cdns.setdefault(cdn, f"{k}: {v}")
    for cdn, line in sorted(cdns.items()):
        emit("digital.cdn", cdn, line, confidence=0.9)

    # 4. software past its vendor's end of support
    for name, software, eol in outdated_server_software(headers, now):
        quote = f"{_original_case(headers, name)}: {headers[_original_case(headers, name)]}"
        emit("digital.outdated_software", f"{software}: upstream support ended {eol}", quote, confidence=0.85,
             notes="End-of-support date from the vendor lifecycle page (table in intel/collectors/site_headers.py). "
                   "A distribution may still back-port security fixes to an ended upstream branch.")

    # 5. security headers: which of the six are present, and is the policy meaningful
    present = [label for label, h in SECURITY_HEADERS if lowered.get(h)]
    missing = [label for label, h in SECURITY_HEADERS if not lowered.get(h)]
    weak = " (CSP allows any script source and inline scripts)" if lowered.get("content-security-policy") and \
        csp_is_permissive(lowered["content-security-policy"]) else ""
    summary = f"{len(present)} of {len(SECURITY_HEADERS)} present: " + (", ".join(present) or "none") + weak + \
              ("; missing: " + ", ".join(missing) if missing else "")
    emit("digital.security_headers", summary, quote_prefix(text), confidence=0.9,
         notes="Derived from the full response header set of the homepage; the quote shows its first lines.")
    if lowered.get("strict-transport-security"):
        emit("digital.hsts", lowered["strict-transport-security"].strip(),
             f"{_original_case(headers, 'strict-transport-security')}: {lowered['strict-transport-security']}")

    # 6. TLS: certificate, issuer, expiry (one handshake to the company's own server)
    if page.url.startswith("https://"):
        if getattr(ctx.fetch, "network_probes", False):
            _tls_facts(sink, page, now)
    else:
        _tls_problem_from_fetch_note(sink, page, asked_url)

    for key in ("digital.web_server", "digital.web_framework", "digital.cms", "digital.cdn"):
        if key not in seen_keys:
            result.checked_absent.append(key)
    return result


def _cookie_excerpt(cookies: str, m: "re.Match[str]") -> str:
    """The cookie name and its `=` only (never the value) as a verbatim piece of the Set-Cookie header."""
    start = m.start()
    while start > 0 and cookies[start - 1] not in " ,;":
        start -= 1
    return cookies[start:m.end()]


def _tls_facts(sink: FactSink, page: Page, now: datetime) -> None:
    host = (urlparse(page.url).netloc or "").split(":")[0]
    if not host:
        return
    probe = tls_probe(host)
    if probe.get("ok"):
        obs = f"{probe['version']} certificate issued by {probe['issuer']}, valid until {probe['not_after']}"
        sink.add("digital.tls_certificate", obs, quote=obs, source_text=obs, url=page.url, confidence=0.9,
                 title="TLS handshake with the company server")
        if probe.get("not_after"):
            days = (datetime.strptime(probe["not_after"], "%Y-%m-%d").replace(tzinfo=timezone.utc) - now).days
            if days < TLS_EXPIRY_WARNING_DAYS:
                text = f"{obs} ({'expired' if days < 0 else 'expires within ' + str(TLS_EXPIRY_WARNING_DAYS) + ' days'})"
                sink.add("digital.tls_problem", f"certificate valid until {probe['not_after']} "
                         f"({'already expired' if days < 0 else 'about to expire'})", quote=obs, source_text=text,
                         url=page.url, confidence=0.9, title="TLS handshake with the company server")
    elif not probe.get("unreachable"):
        sink.add("digital.tls_problem", probe["error"], quote=probe["error"], source_text=probe["error"], url=page.url,
                 confidence=0.85, title="TLS handshake with the company server")


def _tls_problem_from_fetch_note(sink: FactSink, page: Page, asked_url: str) -> None:
    """When the https fetch itself failed with a certificate error, the fetcher's own error text is the evidence."""
    note = getattr(page, "note", "") or ""
    m = re.search(r"(certificate[^'\")]{4,120}|SSLError[^'\")]{4,120}|\bSSL\b[^'\")]{4,120})", note, re.I)
    if m and "https" in (asked_url or "https"):
        sink.add("digital.tls_problem", f"https fails: {m.group(1).strip()[:140]}", quote=m.group(1).strip()[:140],
                 source_text=note, url=asked_url or page.url, confidence=0.8,
                 title="HTTPS request to the company site")


COLLECTOR = Collector(
    name="site-headers", ticket="I02", produces=("digital.",), requires=("web",), refresh_days=180, cost_hint="cheap",
    description="Web server, framework, CDN, hosting panel, end-of-life server software, security headers and the TLS "
                "certificate, read from the homepage response and one TLS handshake.",
    run=run,
)
