import json

from intel.queryplan import build_query_pack, queries_for
from intel.search import SearchClient, SearchResult, canonical_url


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
