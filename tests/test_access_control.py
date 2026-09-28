import pytest
from streamlit.testing.v1 import AppTest

from access_control import password_matches


def test_password_matches_exact_value():
    assert password_matches("a long secret value", "a long secret value")


@pytest.mark.parametrize(
    "candidate",
    ["", "a long secret valu", "a long secret value ", "A long secret value"],
)
def test_password_rejects_non_exact_values(candidate):
    assert not password_matches(candidate, "a long secret value")


def test_login_form_rejects_wrong_password_and_unlocks_with_configured_password(monkeypatch):
    password = "correct-horse-battery-staple"
    monkeypatch.setenv("APP_ACCESS_PASSWORD", password)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from access_control import require_app_access\n"
        "require_app_access()\n"
        "st.success('inside')\n"
    ).run()

    app.text_input[0].input("wrong")
    app.button[0].click()
    app.run()
    assert any("Incorrect password" in error.value for error in app.error)

    app.text_input[0].input(password)
    app.button[0].click()
    app.run()
    assert any(success.value == "inside" for success in app.success)
