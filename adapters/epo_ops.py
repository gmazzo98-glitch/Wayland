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
from datetime import datetime, timedelta, timezone
import requests
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import EPO_OPS_CONSUMER_KEY, EPO_OPS_CONSUMER_SECRET, has_credentials

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


def _fetch_live(company) -> dict:
    query = f'pa="{company.legal_name}"'
    resp = None
    for attempt in range(MAX_SEARCH_ATTEMPTS):
        _check_circuit()
        _wait_for_search_slot()
        token = _get_access_token()
        resp = requests.get(
            SEARCH_URL,
            params={"q": query, "Range": "1-25"},
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
        # OPS returns 404 (not an empty 200) when a search yields zero hits.
        _record_search_success()
        return {
            "signals": {
                "patent_count": {"value": 0.0, "status": "absent"},
                "patent_ipc_diversity": {"value": 0.0, "status": "absent"},
            },
            "raw_payload": {"source": SOURCE_NAME, "query": query, "http_status": 404},
            "confidence": 0.95,
        }
    if resp.status_code >= 400:
        raise EpoOpsUnavailable(f"EPO OPS search failed — {_failure_detail(resp)}")
    _record_search_success()

    root = ET.fromstring(resp.content)
    biblio_search = root.find(".//{*}biblio-search")
    total_count = int(biblio_search.get("total-result-count", "0")) if biblio_search is not None else 0

    ipc_prefixes = set()
    for ipc_text in root.findall(".//{*}classification-ipcr/{*}text"):
        if ipc_text.text:
            ipc_prefixes.add(ipc_text.text.strip()[:4])
    # A patent portfolio exists but this constituent didn't return classification text —
    # report a floor of 1 rather than falsely reading as "zero diversity".
    diversity = float(len(ipc_prefixes)) if ipc_prefixes else (1.0 if total_count > 0 else 0.0)

    status = "present" if total_count > 0 else "absent"
    return {
        "signals": {
            "patent_count": {"value": float(total_count), "status": status},
            "patent_ipc_diversity": {"value": diversity, "status": status},
        },
        "raw_payload": {
            "source": SOURCE_NAME, "query": query, "total_result_count": total_count,
            "ipc_prefixes": sorted(ipc_prefixes),
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
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
        # Concurrent companies may legitimately wait behind the shared OPS request queue.
        timeout=180,
    )
