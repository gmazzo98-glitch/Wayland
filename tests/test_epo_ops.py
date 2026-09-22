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
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = content.decode() if isinstance(content, bytes) else content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def reset_cache():
    epo_ops._reset_token_cache_for_tests()
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
