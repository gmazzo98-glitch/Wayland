"""
Shared execution harness for every ingestion adapter and scraper.

Every source goes through the same honest contract: attempt a real fetch when
credentials/network allow it, otherwise fall back to a clearly-tagged simulated
value. A SignalRecord is never written as if fetched live unless it actually
was — see Section 1 of GG_Dashboard_Technical_Brief.docx ("not a dashboard
over a static dataset").
"""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import Callable, Dict, Any
from sqlalchemy.orm import Session
from models import SignalRecord, SourceHealth

# DNS resolution is not reliably bounded by requests'/urllib3's own `timeout=`
# on every platform (the getaddrinfo() call can block past it) — a single
# slow-resolving or unreachable domain can otherwise hang a whole pipeline run.
# Every fetch_live() call gets a hard wall-clock budget here, independent of
# whatever timeouts the adapter itself sets.
HARD_FETCH_TIMEOUT_SECONDS = 20
_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="adapter-fetch")


def _call_with_hard_timeout(fn: Callable, company, timeout: int = HARD_FETCH_TIMEOUT_SECONDS):
    future = _FETCH_EXECUTOR.submit(fn, company)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        raise TimeoutError(
            f"No response within {timeout}s (a slow/hanging DNS lookup or connection "
            f"is a common cause) — treating this as a failed live attempt."
        )


def get_or_create_source_health(db: Session, source_name: str, phase: int) -> SourceHealth:
    sh = db.query(SourceHealth).filter_by(source_name=source_name).first()
    if not sh:
        sh = SourceHealth(source_name=source_name, phase=phase)
        db.add(sh)
    return sh


def _upsert_signal(db: Session, company_id: str, signal_key: str, source: str,
                    value, status: str, confidence: float, raw_payload: dict,
                    is_simulated: bool):
    sig = db.query(SignalRecord).filter_by(company_id=company_id, signal_key=signal_key).first()
    if not sig:
        sig = SignalRecord(company_id=company_id, signal_key=signal_key, source=source)
        db.add(sig)
    sig.numeric_value = value
    sig.status = status
    sig.confidence = confidence
    sig.source = source
    sig.fetched_at = datetime.utcnow()
    sig.is_simulated = is_simulated
    payload = dict(raw_payload or {})
    payload["simulated"] = is_simulated
    sig.raw_payload_ref = json.dumps(payload, default=str)
    return sig


def run_adapter(
    db: Session,
    company,
    source_name: str,
    phase: int,
    credentials_ok: bool,
    fetch_live: Callable[[Any], Dict[str, Any]],
    simulate: Callable[[Any], Dict[str, Any]],
    cost_per_call: float = 0.0,
) -> Dict[str, Any]:
    """
    fetch_live(company) / simulate(company) must both return:
        {"signals": {signal_key: {"value": float|None, "status": "present"|"absent"}, ...},
         "raw_payload": {...}, "confidence": float}
    fetch_live may raise any Exception on failure (network, auth, parsing) — that's
    expected and handled by falling back to simulate() with the real error surfaced
    on SourceHealth rather than swallowed.
    """
    source_health = get_or_create_source_health(db, source_name, phase)
    source_health.total_calls += 1
    source_health.last_run_at = datetime.utcnow()
    source_health.last_status = "running"
    db.commit()

    used_live = False
    try:
        if credentials_ok:
            result = _call_with_hard_timeout(fetch_live, company)
            used_live = True
        else:
            result = simulate(company)

        for signal_key, sig_data in result["signals"].items():
            _upsert_signal(
                db, company.id, signal_key, source_name,
                sig_data.get("value"), sig_data.get("status", "present"),
                result.get("confidence", 0.9 if used_live else 0.5),
                result.get("raw_payload", {}), is_simulated=not used_live
            )

        source_health.mode = "live" if used_live else "simulated"
        source_health.last_status = "success"
        source_health.last_error_message = None
        if used_live:
            source_health.total_cost += cost_per_call
        db.commit()
        return {"status": "success", "mode": source_health.mode, "signals": result["signals"]}

    except Exception as e:
        db.rollback()
        source_health = get_or_create_source_health(db, source_name, phase)
        source_health.error_count += 1
        source_health.last_status = "error"
        source_health.last_error_message = str(e)[:500]
        db.commit()

        # The live attempt failed — still populate a clearly-tagged simulated value
        # so the dashboard shows an estimate rather than a dead blank, but the error
        # above stays visible on the Pipeline Health page, not silently absorbed.
        try:
            fallback = simulate(company)
            for signal_key, sig_data in fallback["signals"].items():
                _upsert_signal(
                    db, company.id, signal_key, source_name,
                    sig_data.get("value"), sig_data.get("status", "present"),
                    0.5, fallback.get("raw_payload", {}), is_simulated=True
                )
            source_health.mode = "simulated"
            db.commit()
        except Exception as fallback_error:
            db.rollback()
            source_health = get_or_create_source_health(db, source_name, phase)
            source_health.last_error_message = f"{str(e)[:300]} | fallback also failed: {fallback_error}"
            db.commit()

        return {"status": "error", "error": str(e), "mode": "simulated"}
