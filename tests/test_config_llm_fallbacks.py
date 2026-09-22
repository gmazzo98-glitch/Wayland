"""
config._numbered_llm_fallbacks / company_website_crawler's env-passing for LLM_FALLBACK2_*
onward. No real .env involved: a fake getenv/dict stands in.
"""

import config
from scrapers import company_website_crawler as cwc


def test_no_extra_fallbacks_when_none_configured():
    assert config._numbered_llm_fallbacks(getenv={}.get) == []


def test_reads_fallback2_and_fallback3():
    env = {
        "CRAWLER_LLM_FALLBACK2_API_KEY": "k2", "CRAWLER_LLM_FALLBACK2_BASE_URL": "https://two.example/v1",
        "CRAWLER_LLM_FALLBACK3_API_KEY": "k3", "CRAWLER_LLM_FALLBACK3_MODEL": "model-3",
    }
    fallbacks = config._numbered_llm_fallbacks(getenv=env.get)
    assert fallbacks == [
        {"suffix": "2", "api_key": "k2", "base_url": "https://two.example/v1", "model": None},
        {"suffix": "3", "api_key": "k3", "base_url": None, "model": "model-3"},
    ]


def test_a_suffix_with_no_api_key_is_skipped():
    env = {"CRAWLER_LLM_FALLBACK2_BASE_URL": "https://two.example/v1"}  # no key -> not a provider
    assert config._numbered_llm_fallbacks(getenv=env.get) == []


def test_company_website_crawler_passes_extra_fallbacks_through_to_the_subprocess_env(monkeypatch):
    monkeypatch.setattr(cwc, "CRAWLER_LLM_API_KEY", "primary-key")
    monkeypatch.setattr(cwc, "CRAWLER_LLM_FALLBACK_API_KEY", "fallback1-key")
    monkeypatch.setattr(cwc, "CRAWLER_LLM_EXTRA_FALLBACKS", [
        {"suffix": "2", "api_key": "fallback2-key", "base_url": "https://two.example/v1", "model": None},
    ])
    captured = {}

    def fake_run_ts_crawler(crawler_dir, rows, env_overrides=None, run_timeout=None):
        captured["env"] = env_overrides
        return [{"company_id": rows[0]["company_id"], "field_status": {}}]

    monkeypatch.setattr(cwc, "run_ts_crawler", fake_run_ts_crawler)
    # Bypass run_adapter's DB/timeout machinery entirely: just call the fetch_live it was given,
    # exactly like a live credentials_ok=True call would, with no session required.
    monkeypatch.setattr(cwc, "run_adapter", lambda db, company, *a, fetch_live, **k: fetch_live(company))
    monkeypatch.setattr(cwc, "save_crawler_blob", lambda *a, **k: None)

    class _Co:
        id, website_url = "c1", "https://example.com"

    cwc.sync_company_website(_Co(), db_session=None)
    assert captured["env"]["LLM_FALLBACK2_API_KEY"] == "fallback2-key"
    assert captured["env"]["LLM_FALLBACK2_BASE_URL"] == "https://two.example/v1"
    assert "LLM_FALLBACK2_MODEL" not in captured["env"]
