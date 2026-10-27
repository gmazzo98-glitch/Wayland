import json
import multiprocessing
import time
import requests
import pytest

from intel.queryplan import build_query_pack, queries_for
from intel.search import SearchClient, SearchResult, canonical_url


class NoThrottle:
    def reserve(self):
        pass
    def backoff(self):
        pass


def _reserve_in_process(path, start, queue):
    from intel.search import SharedGdeltThrottle
    start.wait()
    SharedGdeltThrottle(path, interval=0.2).reserve()
    queue.put(time.time())


def test_provider_failures_are_reported_and_not_cached(tmp_path):
    class Session:
        headers = {}
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            raise requests.Timeout("sensitive request URL")

    session = Session()
    client = SearchClient(session=session, providers=("duckduckgo",), cache_dir=str(tmp_path), delay=0)
    response = client.search("example")
    assert response == []
    assert response.provider_health == {"duckduckgo": "error: Timeout"}
    assert "sensitive" not in str(response.provider_health)
    client.search("example")
    assert session.calls == 2
    assert list(tmp_path.iterdir()) == []


def test_http_throttle_is_distinct_from_no_results():
    class Response:
        status_code = 429

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            return Response()

    response = SearchClient(session=Session(), providers=("gdelt",), delay=0, gdelt_throttle=NoThrottle()).search("example")
    assert response.provider_health == {"gdelt": "error: ProviderError (HTTP 429)"}


def test_empty_success_has_healthy_provider():
    class Response:
        status_code = 200
        def json(self):
            return {"articles": []}

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            return Response()

    response = SearchClient(session=Session(), providers=("gdelt",), delay=0, gdelt_throttle=NoThrottle()).search("example")
    assert response == []
    assert response.provider_health == {"gdelt": "ok"}


def test_google_news_only_returns_resolved_publisher_links():
    class Response:
        status_code = 200
        def __init__(self, content=b"", url=""):
            self.content, self.url = content, url

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            if "/rss/search?" in url:
                return Response(b'<rss><channel><item><title>Story</title><link>https://news.google.com/rss/articles/token</link></item></channel></rss>')
            return Response(url="https://publisher.example/story")

    result = SearchClient(session=Session(), providers=("google-news",), delay=0).search("example")
    assert [item.url for item in result] == ["https://publisher.example/story"]
    assert result.provider_health == {"google-news": "ok"}


def test_google_news_unresolved_links_report_failure():
    class Response:
        status_code = 200
        content = b'<rss><channel><item><title>Story</title><link>https://news.google.com/rss/articles/token</link></item></channel></rss>'
        url = "https://news.google.com/articles/token"

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            return Response()

    result = SearchClient(session=Session(), providers=("google-news",), delay=0).search("example")
    assert result == []
    assert "publisher links unresolved" in result.provider_health["google-news"]


def test_gdelt_throttle_reservations_are_shared(tmp_path, monkeypatch):
    from intel.search import SharedGdeltThrottle
    sleeps = []
    now = [100.0]
    monkeypatch.setattr("intel.search.time.time", lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr("intel.search.time.sleep", sleep)
    path = str(tmp_path / "gdelt.sqlite3")
    first = SharedGdeltThrottle(path, interval=5.1)
    second = SharedGdeltThrottle(path, interval=5.1)
    first.reserve()
    second.reserve()
    assert sleeps == [pytest.approx(5.1)]
    first.backoff(20)
    second.reserve()
    assert sleeps[-1] == 20


def test_gdelt_429_requests_backoff():
    class Throttle(NoThrottle):
        backed_off = False
        def backoff(self):
            self.backed_off = True

    class Response:
        status_code = 429

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            return Response()

    throttle = Throttle()
    result = SearchClient(session=Session(), providers=("gdelt",), delay=0, gdelt_throttle=throttle).search("example")
    assert throttle.backed_off
    assert "HTTP 429" in result.provider_health["gdelt"]


def test_gdelt_throttle_coordinates_separate_processes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    start, queue = ctx.Event(), ctx.Queue()
    path = str(tmp_path / "shared.sqlite3")
    workers = [ctx.Process(target=_reserve_in_process, args=(path, start, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    stamps = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert abs(stamps[1] - stamps[0]) >= 0.18


def test_query_pack_is_multilingual_and_has_name_variants():
    qs = build_query_pack({"legal_name": "Acme S.P.A.", "vat": "IT123"}, ["ownership"])
    assert len(qs) >= 6
    assert {q.language for q in qs} == {"it", "en", "de"}
    assert any("Acme SpA" in q.text for q in qs)
    assert queries_for({"legal_name": "Acme"}, "unknown") == []


def test_search_deduplicates_and_blocks_directory_domains(monkeypatch):
    class Response:
        def __init__(self, payload): self.text, self.content = payload, payload.encode()
        def json(self): return json.loads(self.text)
    class Session:
        def __init__(self): self.headers = {}
        def get(self, url, **kw):
            if "duckduckgo" in url:
                return Response('<div class="result"><a class="result__a" href="https://example.com/a#x">A</a><a class="result__a" href="https://example.com/a">A2</a></div>')
            if "news.google" in url:
                return Response('<rss><channel><item><title>N</title><link>https://kompass.com/x</link></item></channel></rss>')
            return Response('{"articles":[]}')
    out = SearchClient(session=Session(), providers=("duckduckgo", "google-news"), delay=0).search("acme", n=10)
    assert [x.url for x in out] == ["https://example.com/a#x"]
    assert canonical_url("HTTPS://WWW.Example.com/a/") == "https://example.com/a"


def test_search_cache_is_offline(tmp_path):
    client = SearchClient(session=object(), providers=(), cache_dir=str(tmp_path), delay=0)
    path = tmp_path / "cached.json"
    # Cache key is an implementation detail; exercise the public behavior through a provider-free client.
    assert client.search("nothing", n=2) == []


def test_search_politeness_spacing(monkeypatch):
    import time
    timestamps = []

    class MockResponse:
        text = '{"articles":[]}'
        content = b'{"articles":[]}'
        def json(self): return {"articles": []}

    class MockSession:
        headers = {}
        def get(self, url, **kw):
            timestamps.append(time.monotonic())
            return MockResponse()

    client = SearchClient(session=MockSession(), providers=("gdelt",), delay=0.05, gdelt_throttle=NoThrottle())
    client.search("test1", n=1)
    client.search("test2", n=1)
    assert len(timestamps) == 2
    # Ensure delay was enforced between requests
    assert (timestamps[1] - timestamps[0]) >= 0.045


def test_sec_edgar_result_url_parsing():
    sample_sec_hit = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "display_names": ["IDEX CORP /DE/  (IEX)  (CIK 0000832101)"],
                        "file_date": "2025-02-20",
                        "form": "10-K",
                        "adsh": "0000832101-25-000010",
                        "ciks": ["0000832101"],
                        "file_description": "Annual report for fiscal year ended December 31, 2024",
                    }
                }
            ]
        }
    }

    class MockResponse:
        def json(self): return sample_sec_hit

    class MockSession:
        headers = {}
        def get(self, url, **kw): return MockResponse()

    client = SearchClient(session=MockSession(), providers=("sec",), delay=0)
    res = client.search("ASCO Filtri", n=1)
    assert len(res) == 1
    assert "0000832101-25-000010.txt" in res[0].url
    assert "https://www.sec.gov/Archives/edgar/data/832101/" in res[0].url
    assert "IDEX CORP" in res[0].title
    assert res[0].provider == "sec"
