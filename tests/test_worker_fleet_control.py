import base64
import json
from unittest.mock import Mock

import pytest

import worker_fleet_control as fleet


@pytest.fixture
def github(monkeypatch):
    entry = Mock(status_code=200)
    entry.json.return_value = {
        "encoding": "base64",
        "sha": "a" * 40,
        "content": base64.b64encode(b'{"paused": false, "extra": 1}').decode(),
    }
    get = Mock(return_value=entry)
    put = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(fleet.requests, "get", get)
    monkeypatch.setattr(fleet.requests, "put", put)
    return get, put


def test_request_changes_only_the_fleet_mode(github):
    get, put = github
    fleet.request_fleet_mode(True, "test-token")
    assert get.call_args.kwargs["params"] == {"ref": "master"}
    args, kwargs = put.call_args
    assert args[0].endswith("/contents/worker_shim/app/fleet_mode.json")
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    body = kwargs["json"]
    assert body["sha"] == "a" * 40 and body["branch"] == "master"
    assert json.loads(base64.b64decode(body["content"])) == {"paused": True, "extra": 1}


def test_repeat_request_skips_the_commit(github):
    _, put = github
    fleet.request_fleet_mode(False, "test-token")
    put.assert_not_called()


def test_conflicting_request_does_not_overwrite_the_flag(github):
    _, put = github
    put.return_value.status_code = 409
    with pytest.raises(fleet.FleetControlError, match="changed during the request"):
        fleet.request_fleet_mode(True, "test-token")
    put.assert_called_once()


def test_app_button_waits_for_live_mode_before_showing_success(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from views import crawler_setup

    request = Mock()
    monkeypatch.setattr(crawler_setup, "request_fleet_mode", request)
    monkeypatch.setattr(crawler_setup, "get_config_var", lambda key: "test-token")
    app = AppTest.from_string("""
import streamlit as st
from views.crawler_setup import _render_fleet_control
_render_fleet_control(st.session_state.get("live_paused", False))
""").run()
    app.button(key="change_crawler_fleet_mode").click().run()
    request.assert_called_once_with(True, "test-token")
    assert "Waiting for Vercel" in app.info[0].value
    assert not app.success

    app.session_state["live_paused"] = True
    app.run()
    assert app.success[0].value == "Remote workers are paused."
    assert app.button(key="change_crawler_fleet_mode").label == "Resume remote workers"
