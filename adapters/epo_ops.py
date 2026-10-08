"""
EPO OPS Patent Data Ingestion Adapter (Phase 1 API).
Real REST integration: OAuth2 client-credentials token, then a published-data
biblio search by applicant name. Populates patent_count and patent_ipc_diversity
from the single search call. Requires EPO_OPS_CONSUMER_KEY/SECRET (free
registration at https://www.epo.org/en/searching-for-patents/data/web-services/ops) —
falls back to a clearly-tagged simulated value when absent. See adapters/base.py.
"""

import threading
import time
import re
from datetime import datetime, timedelta, timezone
import requests
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import EPO_OPS_CONSUMER_KEY, EPO_OPS_CONSUMER_SECRET, has_credentials
from models import RawImportRecord

SOURCE_NAME = "EPO OPS"
PHASE = 1
TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"

# EPO OPS access tokens are valid ~20 minutes; this adapter used to request a brand new one on
# EVERY company's search call. That's harmless sequentially, but a bulk run fires this from
# several threads at once (company_service.run_company_phases's concurrent Phase 1 steps, times
# `workers` companies in flight) — verified live 2026-09-22: a 953-company batch at 7-10 workers
# made EPO OPS's token endpoint start answering 403 Forbidden for the rest of the run (634 of 811
# calls), which OPS's own docs attribute to hitting its throttle on repeated token issuance, a
# separate limit from the search endpoint's own quota. Caching the token process-wide (guarded by
# a lock so concurrent threads share one refresh instead of each fetching their own) removes that
# self-inflicted load entirely.
_token_lock = threading.Lock()
_cached_token = None
_token_expires_at = 0.0

# OPS's published fair-use guidance says search traffic normally tolerates about ten searches
# per minute per IP (and may tighten that dynamically). Company crawls run concurrently, so this
# scheduler is process-wide. 6.5s leaves a little margin below ten/minute.
SEARCH_INTERVAL_SECONDS = 6.5
MAX_SEARCH_ATTEMPTS = 3
_search_lock = threading.Lock()
_next_search_at = 0.0
_search_interval = SEARCH_INTERVAL_SECONDS

# Stop a bulk run from turning one quota rejection into hundreds of doomed requests.
_circuit_lock = threading.Lock()
_circuit_open_until = 0.0
_circuit_reason = None
_consecutive_rejections = 0


class EpoOpsUnavailable(RuntimeError):
    """An actionable OPS failure suitable for Pipeline Health's Last Error field."""


def _invalidate_token() -> None:
    global _cached_token, _token_expires_at
    with _token_lock:
        _cached_token, _token_expires_at = None, 0.0


def _wait_for_search_slot() -> None:
    """Reserve one globally spaced search slot without sleeping while holding the lock."""
    global _next_search_at
    with _search_lock:
        now = time.monotonic()
        slot = max(now, _next_search_at)
        _next_search_at = slot + _search_interval
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


def _apply_throttling_hint(response) -> None:
    """Slow future calls when OPS's self-throttling header marks search yellow/red."""
    global _search_interval
    hint = (response.headers.get("X-Throttling-Control", "") if response.headers else "").lower()
    with _search_lock:
        if "search=red" in hint:
            _search_interval = max(_search_interval, 15.0)
        elif "search=yellow" in hint:
            _search_interval = max(_search_interval, 9.0)
        elif "search=green" in hint or hint.startswith("idle"):
            _search_interval = SEARCH_INTERVAL_SECONDS


def _weekly_reset_delay() -> float:
    now = datetime.now(timezone.utc)
    next_monday = (now + timedelta(days=(7 - now.weekday()))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60.0, (next_monday - now).total_seconds())


def _check_circuit() -> None:
    with _circuit_lock:
        if time.time() < _circuit_open_until:
            remaining = max(1, int(_circuit_open_until - time.time()))
            raise EpoOpsUnavailable(
                f"EPO OPS requests paused for another {remaining}s: {_circuit_reason}"
            )


def _record_search_success() -> None:
    global _consecutive_rejections
    with _circuit_lock:
        _consecutive_rejections = 0


def _record_rejection(reason: str) -> None:
    global _consecutive_rejections, _circuit_open_until, _circuit_reason
    normalised = (reason or "OPS rejected the request").strip()
    with _circuit_lock:
        _consecutive_rejections += 1
        lower = normalised.lower()
        if "week" in lower:
            delay = _weekly_reset_delay()
        elif "hour" in lower:
            delay = 3600.0
        elif _consecutive_rejections >= 3:
            delay = 900.0
        else:
            return
        _circuit_open_until = time.time() + delay
        _circuit_reason = normalised


def _failure_detail(response) -> str:
    rejection = response.headers.get("X-Rejection-Reason") if response.headers else None
    quota = []
    for name in ("X-IndividualQuotaPerHour-Used", "X-RegisteredQuotaPerWeek-Used"):
        if response.headers and response.headers.get(name):
            quota.append(f"{name}={response.headers[name]}")
    detail = rejection or (response.text or "").strip()[:200] or response.reason or "no detail"
    return f"HTTP {response.status_code}: {detail}" + (f" ({', '.join(quota)})" if quota else "")


def _get_access_token() -> str:
    global _cached_token, _token_expires_at
    with _token_lock:
        if _cached_token and time.time() < _token_expires_at:
            return _cached_token
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(EPO_OPS_CONSUMER_KEY, EPO_OPS_CONSUMER_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # expires_in is seconds, per OPS's docs (typically 1200 = 20 minutes); refresh a minute
        # early so a token doesn't expire mid-flight under a slow concurrent call.
        expires_in = int(data.get("expires_in", 1200))
        _cached_token = data["access_token"]
        _token_expires_at = time.time() + max(60, expires_in - 60)
        return _cached_token


def _reset_token_cache_for_tests() -> None:
    """Test seam: reset all process-wide OPS state."""
    global _cached_token, _token_expires_at, _next_search_at, _search_interval
    global _circuit_open_until, _circuit_reason, _consecutive_rejections
    with _token_lock:
        _cached_token, _token_expires_at = None, 0.0
    with _search_lock:
        _next_search_at, _search_interval = 0.0, SEARCH_INTERVAL_SECONDS
    with _circuit_lock:
        _circuit_open_until, _circuit_reason, _consecutive_rejections = 0.0, None, 0


def _xml_text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _patent_sample(documents: list) -> list[dict]:
    """Keep verifiable bibliographic context from the search page, not a portfolio claim."""
    sample = []
    for doc in documents:
        publication = "".join(doc.get(part, "") for part in ("country", "doc-number", "kind"))
        if not publication:
            continue
        titles = doc.findall(".//{*}invention-title")
        title_node = next((n for n in titles if n.get("lang") == "en"), titles[0] if titles else None)
        abstract_node = next((n for n in doc.findall(".//{*}abstract") if n.get("lang") == "en"), None)
        original_applicants = [
            _xml_text(n) for n in doc.findall(".//{*}applicant")
            if n.get("data-format") == "original" and _xml_text(n)
        ]
        date = doc.find(".//{*}publication-reference/{*}document-id/{*}date")
        sample.append({
            "publication": publication,
            "publication_date": _xml_text(date),
            "family_id": doc.get("family-id"),
            "title": _xml_text(title_node),
            "abstract_excerpt": _xml_text(abstract_node)[:500],
            "applicants": list(dict.fromkeys(original_applicants))[:8],
        })
    sample.sort(key=lambda item: item["publication_date"], reverse=True)
    return sample[:15]


def _fetch_live(company) -> dict:
    query = f'pa="{company.legal_name}"'
    resp = None
    for attempt in range(MAX_SEARCH_ATTEMPTS):
        _check_circuit()
        _wait_for_search_slot()
        token = _get_access_token()
        resp = requests.get(
            SEARCH_URL,
            params={"q": query, "Range": "1-100"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        _apply_throttling_hint(resp)

        if resp.status_code == 401 and attempt == 0:
            _invalidate_token()
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt + 1 < MAX_SEARCH_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After") if resp.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 5.0 * (2 ** attempt)
                time.sleep(min(delay, 60.0))
                continue
        if resp.status_code == 403:
            detail = _failure_detail(resp)
            _record_rejection(detail)
            rejection = (resp.headers.get("X-Rejection-Reason", "") if resp.headers else "").lower()
            if "week" not in rejection and attempt + 1 < MAX_SEARCH_ATTEMPTS:
                time.sleep(10.0 * (2 ** attempt))
                continue
            raise EpoOpsUnavailable(f"EPO OPS search rejected — {detail}")
        break

    if resp.status_code == 404:
        # No hit for one legal-name spelling does not rule out filings under a
        # former name, subsidiary, parent or trading name. Preserve the lookup
        # in SourceHealth without scoring a fabricated portfolio absence.
        _record_search_success()
        return {
            "signals": {},
            "raw_payload": {"source": SOURCE_NAME, "query": query, "http_status": 404,
                            "note": "No match for this name; aliases and group companies were not checked"},
            "confidence": 0.0,
        }
    if resp.status_code >= 400:
        raise EpoOpsUnavailable(f"EPO OPS search failed — {_failure_detail(resp)}")
    _record_search_success()

    root = ET.fromstring(resp.content)
    biblio_search = root.find(".//{*}biblio-search")
    total_count = int(biblio_search.get("total-result-count", "0")) if biblio_search is not None else 0

    returned_documents = root.findall(".//{*}exchange-document")
    patent_sample = _patent_sample(returned_documents)
    ipc_prefixes = set()
    for ipc_text in root.findall(".//{*}classification-ipcr/{*}text"):
        if ipc_text.text:
            ipc_prefixes.add(ipc_text.text.strip()[:4])
    signals = {
        "patent_count": {"value": float(total_count), "status": "present",
                         "summary": f"{total_count} matching patent publication record(s) for {query}; not deduplicated by invention or family"},
    } if total_count > 0 else {}
    # IPC diversity is portfolio-wide only if every result was retrieved and
    # classified. A first-page sample must never masquerade as the full set.
    if total_count > 0 and len(returned_documents) >= total_count and ipc_prefixes:
        signals["patent_ipc_diversity"] = {"value": float(len(ipc_prefixes)), "status": "present"}
    return {
        "signals": signals,
        "raw_payload": {
            "source": SOURCE_NAME, "query": query, "total_result_count": total_count,
            "returned_documents": len(returned_documents), "ipc_prefixes": sorted(ipc_prefixes),
            "ipc_diversity_complete": "patent_ipc_diversity" in signals,
            "publication_sample": patent_sample,
            "sample_scope": "Up to 15 publications from the first 100 search results, sorted by publication date; not a complete patent-family portfolio",
        },
        "confidence": 0.95,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    if char_sum % 5 == 0:
        patent_val, ipc_val, status = 0.0, 0.0, "absent"
    else:
        patent_val = float((char_sum % 7) + 1)
        ipc_val = float((char_sum % 3) + 1)
        status = "present"
    return {
        "signals": {
            "patent_count": {"value": patent_val, "status": status},
            "patent_ipc_diversity": {"value": ipc_val, "status": status},
        },
        "raw_payload": {
            "source": SOURCE_NAME, "query": company.legal_name,
            "note": "EPO_OPS_CONSUMER_KEY/SECRET not configured",
        },
        "confidence": 0.5,
    }


def sync_company_patents(company, db_session: Session) -> dict:
    """Populates patent_count and patent_ipc_diversity from one EPO OPS search call."""
    captured = {}

    def fetch(c):
        result = _fetch_live(c)
        captured["payload"] = result["raw_payload"]
        return result

    outcome = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=fetch, simulate=_simulate,
        # Concurrent companies may legitimately wait behind the shared OPS request queue.
        timeout=180,
    )
    if captured:
        rec = db_session.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name="crawler_epo_ops_search").first()
        if rec is None:
            rec = RawImportRecord(company_id=company.id, dataset_name="crawler_epo_ops_search")
            db_session.add(rec)
        rec.source_filename = "EPO OPS applicant search"
        rec.raw_row = captured["payload"]
        rec.updated_at = datetime.utcnow()
        db_session.commit()
    return outcome
