"""The public update route must serve the exact bytes advertised to workers."""

import hashlib
import runpy
from pathlib import Path

from fastapi.testclient import TestClient


def test_update_manifest_and_bundle_share_a_release(monkeypatch):
    import psycopg2.pool

    def unexpected_connection(*args):
        raise AssertionError("Control and update routes must stay database-free")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(psycopg2.pool, "SimpleConnectionPool", unexpected_connection)
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


def test_fleet_pause_blocks_idle_sql_and_keeps_task_completion_available(monkeypatch):
    import psycopg2.pool
    from unittest.mock import MagicMock

    pool = MagicMock()
    connection = pool.getconn.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ({"ok": True},)
    connect = MagicMock(return_value=pool)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(psycopg2.pool, "SimpleConnectionPool", connect)
    shim = runpy.run_path(str(Path(__file__).resolve().parents[1] / "worker_shim" / "app" / "main.py"))
    globals_ = shim["rpc"].__globals__
    globals_["FLEET_PAUSED"] = True
    client = TestClient(shim["app"])

    status = client.get("/fleet")
    assert status.json() == {"paused": True}
    assert status.headers["Cache-Control"] == "no-store"
    assert client.post("/rpc/vienna_worker_claim", json={"p_token": "test"}).json() == {
        "ok": True, "task": None, "fleet_paused": True,
    }
    assert client.post("/rpc/vienna_worker_heartbeat", json={"p_token": "test", "p_info": {}}).json()["fleet_paused"]
    report = client.post("/rpc/vienna_worker_site_evidence_report", json={"p_token": "test", "p_report": {}})
    assert report.status_code == 503
    connect.assert_not_called()

    response = client.post("/rpc/vienna_worker_complete", json={
        "p_token": "test", "p_task_id": 1, "p_ok": True, "p_result": {}, "p_error": None,
    })
    assert response.json() == {"ok": True}
    connect.assert_called_once_with(0, 10, "postgresql://unused")
    pool.putconn.assert_called_once_with(connection, close=False)

    # A new deployment with the mode enabled starts with no connection pool.
    globals_.update(FLEET_PAUSED=False, pool=None)
    connect.reset_mock()
    assert client.get("/fleet").json() == {"paused": False}
    connect.assert_not_called()
    cursor.fetchone.return_value = ({"ok": True, "task": {"id": 2}},)
    assert client.post("/rpc/vienna_worker_claim", json={"p_token": "test"}).json()["task"] == {"id": 2}
    connect.assert_called_once_with(1, 10, "postgresql://unused")
