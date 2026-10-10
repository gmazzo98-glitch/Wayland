"""The public update route must serve the exact bytes advertised to workers."""

import hashlib
import runpy
from pathlib import Path

from fastapi.testclient import TestClient


def test_update_manifest_and_bundle_share_a_release(monkeypatch):
    import psycopg2.pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(psycopg2.pool, "SimpleConnectionPool", lambda *a, **k: object())
    shim = runpy.run_path(str(Path(__file__).resolve().parents[1] / "worker_shim" / "app" / "main.py"))
    client = TestClient(shim["app"], base_url="https://shim.example.com")

    manifest_response = client.get("/update/latest")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["download_url"] == f"https://shim.example.com/update/bundle/{manifest['build']}"

    bundle = client.get(manifest["download_url"])
    assert bundle.status_code == 200
    assert len(bundle.content) == manifest["size_bytes"]
    assert hashlib.sha256(bundle.content).hexdigest() == manifest["sha256"]
    assert client.get("/update/bundle/obsolete").status_code == 404

    shim["checked_update_bundle"].__globals__["UPDATE_BUNDLE"] = Path("missing-crawler-bundle.zip")
    assert client.get("/update/latest").status_code == 503
