"""
config._numbered_llm_fallbacks / company_website_crawler's env-passing for LLM_FALLBACK2_*
onward. No real .env involved: a fake getenv/dict stands in.
"""

import config
from scrapers import company_website_crawler as cwc


def test_no_extra_fallbacks_when_none_configured():
    assert config._numbered_llm_fallbacks(getenv={}.get) == []


def test_crawler_llm_settings_reflects_an_env_change_without_reimporting_anything(monkeypatch):
    """The actual live bug this ticket fixes (docs/CRAWLER_ROADMAP.md T24): Company Website
    Crawler kept reporting "simulated" even after CRAWLER_LLM_API_KEY was already present in
    .env, because the OLD code did `from config import CRAWLER_LLM_API_KEY` at
    company_website_crawler.py's own module top level - a statement that runs exactly once,
    the first time anything imports that module, permanently binding whatever os.getenv() saw
    at THAT moment. This proves the replacement (config.crawler_llm_settings(), called fresh
    every time) does NOT have that problem: changing the env mid-process, with no re-import
    anywhere, must be visible on the very next call."""
    monkeypatch.delenv("CRAWLER_LLM_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # isolate from the real .env file
    assert config.crawler_llm_settings()["api_key"] is None

    monkeypatch.setenv("CRAWLER_LLM_API_KEY", "a-key-that-was-just-added")
    assert config.crawler_llm_settings()["api_key"] == "a-key-that-was-just-added"


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
    # cwc reads config.crawler_llm_settings() fresh on every call now (see that function's own
    # docstring for why - a plain module-level CRAWLER_LLM_API_KEY stayed stuck on whatever
    # was configured the first time anything imported this module, even after a real key was
    # later added to .env), so controlling what it sees means monkeypatching that function,
    # not module attributes that no longer exist on cwc at all.
    monkeypatch.setattr(config, "crawler_llm_settings", lambda: {
        "api_key": "primary-key", "base_url": "https://api.groq.com/openai/v1", "model": "some-model",
        "fallback_api_key": "fallback1-key", "fallback_base_url": None, "fallback_model": None,
        "extra_fallbacks": [
            {"suffix": "2", "api_key": "fallback2-key", "base_url": "https://two.example/v1", "model": None},
        ],
    })
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
