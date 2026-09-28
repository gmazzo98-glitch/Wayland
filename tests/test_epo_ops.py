"""
adapters/epo_ops.py's OAuth token cache. Verified live 2026-09-22: requesting a brand new
access token on every company's search call made EPO OPS throttle the token endpoint itself
under bulk concurrent load — 634 of 811 calls in a 953-company batch came back 403 Forbidden,
separate from the search endpoint's own quota. No network here: requests.post/get are stubbed.
"""

import threading
import time

import pytest
import requests

from adapters import epo_ops


class _Co:
    def __init__(self, legal_name="Test SRL"):
        self.legal_name = legal_name


class _Resp:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None, reason=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = content.decode() if isinstance(content, bytes) else content
        self.headers = headers or {}
        self.reason = reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    epo_ops._reset_token_cache_for_tests()
    # These tests exercise response policy rather than real wall-clock pacing.
    monkeypatch.setattr(epo_ops, "_wait_for_search_slot", lambda: None)
    yield
    epo_ops._reset_token_cache_for_tests()


def test_token_is_cached_across_calls(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp(200, {"access_token": "tok-1", "expires_in": 1200})

    monkeypatch.setattr(epo_ops.requests, "post", fake_post)
    t1 = epo_ops._get_access_token()
    t2 = epo_ops._get_access_token()
    assert t1 == t2 == "tok-1"
    assert len(calls) == 1, "a second call within the token's lifetime must not re-fetch"


def test_token_refreshes_after_expiry(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp(200, {"access_token": f"tok-{len(calls)}", "expires_in": 1})

    monkeypatch.setattr(epo_ops.requests, "post", fake_post)
    t1 = epo_ops._get_access_token()
    epo_ops._token_expires_at = time.time() - 1  # force expiry without a real sleep
    t2 = epo_ops._get_access_token()
    assert t1 == "tok-1" and t2 == "tok-2"
    assert len(calls) == 2


def test_concurrent_calls_share_one_token_fetch(monkeypatch):
    """The exact scenario that broke live: several of a bulk run's worker threads calling this
    at once must collapse into ONE token request, not one each."""
    calls = []
    call_lock = threading.Lock()

    def fake_post(url, **kwargs):
        with call_lock:
            calls.append(url)
        time.sleep(0.05)  # widens the race window so concurrent callers would collide without the lock
        return _Resp(200, {"access_token": "tok-shared", "expires_in": 1200})

    monkeypatch.setattr(epo_ops.requests, "post", fake_post)
    tokens, tok_lock = [], threading.Lock()

    def worker():
        t = epo_ops._get_access_token()
        with tok_lock:
            tokens.append(t)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert tokens == ["tok-shared"] * 8


def test_fetch_live_sends_the_cached_token(monkeypatch):
    monkeypatch.setattr(epo_ops, "_get_access_token", lambda: "tok-xyz")
    captured = {}

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _Resp(404)  # OPS's own "zero hits" signal

    monkeypatch.setattr(epo_ops.requests, "get", fake_get)

    result = epo_ops._fetch_live(_Co())
    assert captured["headers"]["Authorization"] == "Bearer tok-xyz"
    assert result["signals"]["patent_count"] == {"value": 0.0, "status": "absent"}


def test_401_refreshes_token_once(monkeypatch):
    tokens = iter(("expired", "fresh"))
    monkeypatch.setattr(epo_ops, "_get_access_token", lambda: next(tokens))
    monkeypatch.setattr(epo_ops, "_invalidate_token", lambda: None)
    seen = []

    def fake_get(url, **kwargs):
        seen.append(kwargs["headers"]["Authorization"])
        return _Resp(401) if len(seen) == 1 else _Resp(404)

    monkeypatch.setattr(epo_ops.requests, "get", fake_get)
    epo_ops._fetch_live(_Co())
    assert seen == ["Bearer expired", "Bearer fresh"]


def test_weekly_quota_opens_circuit_without_retries(monkeypatch):
    monkeypatch.setattr(epo_ops, "_get_access_token", lambda: "tok")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp(403, headers={"X-Rejection-Reason": "RegisteredQuotaPerWeek"})

    monkeypatch.setattr(epo_ops.requests, "get", fake_get)
    with pytest.raises(epo_ops.EpoOpsUnavailable, match="RegisteredQuotaPerWeek"):
        epo_ops._fetch_live(_Co())
    with pytest.raises(epo_ops.EpoOpsUnavailable, match="paused"):
        epo_ops._fetch_live(_Co("Another SRL"))
    assert len(calls) == 1


def test_temporary_failure_retries_then_recovers(monkeypatch):
    monkeypatch.setattr(epo_ops, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(epo_ops.time, "sleep", lambda _: None)
    responses = iter((_Resp(503), _Resp(429), _Resp(404)))
    monkeypatch.setattr(epo_ops.requests, "get", lambda *a, **kw: next(responses))
    result = epo_ops._fetch_live(_Co())
    assert result["signals"]["patent_count"]["value"] == 0.0


def test_throttling_header_slows_and_green_restores_scheduler():
    epo_ops._apply_throttling_hint(_Resp(headers={"X-Throttling-Control": "search=red:30"}))
    assert epo_ops._search_interval == 15.0
    epo_ops._apply_throttling_hint(_Resp(headers={"X-Throttling-Control": "search=green:200"}))
    assert epo_ops._search_interval == epo_ops.SEARCH_INTERVAL_SECONDS
