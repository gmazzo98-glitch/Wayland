from intel.fetch import Page, PoliteFetcher


def test_fetcher_cache_round_trip_without_network(tmp_path):
    class Raw:
        def read(self, *args, **kwargs): return b"<html><title>T</title><p>Hello</p></html>"
    class Response:
        status_code = 200; url = "https://example.test/"; encoding = "utf-8"; apparent_encoding = "utf-8"
        headers = {"Content-Type": "text/html", "ETag": '"one"'}; raw = Raw()
    class Session:
        def __init__(self): self.headers = {}; self.calls = 0
        def get(self, url, **kwargs): self.calls += 1; return Response()
    session = Session()
    fetcher = PoliteFetcher(session=session, respect_robots=False, delay=0, cache_dir=str(tmp_path))
    first = fetcher.fetch("https://example.test/")
    assert first.ok and "Hello" in first.text
    second = fetcher.fetch("https://example.test/")
    assert second.text == first.text
    assert session.calls == 1  # the fresh cache avoids a second network request
