"""Application-level access control for the public Streamlit deployment.

Streamlit Community Cloud can serve an app from a public repository, but that
does not mean the app's operational controls should be public.  The shared
password is supplied through Streamlit Secrets (or an environment variable for
local development) and is never committed to the repository.
"""

import hmac
import os

import streamlit as st


PASSWORD_ENV_VAR = "APP_ACCESS_PASSWORD"
MIN_PASSWORD_LENGTH = 16
_AUTHENTICATED_KEY = "_vienna_access_granted"


def _configured_password() -> str | None:
    """Read the password without ever logging or rendering its value."""
    value = os.getenv(PASSWORD_ENV_VAR)
    if value:
        return value
    try:
        value = st.secrets.get(PASSWORD_ENV_VAR)
    except Exception:
        value = None
    return str(value) if value else None


def password_matches(candidate: str, configured: str) -> bool:
    """Compare credentials without leaking matching-prefix timing information."""
    return hmac.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))


def require_app_access() -> None:
    """Stop the Streamlit run unless this browser session has authenticated."""
    configured = _configured_password()
    if not configured:
        st.error(
            "Access control is not configured. Add `APP_ACCESS_PASSWORD` to "
            "Streamlit Secrets before using this app."
        )
        st.stop()
    if len(configured) < MIN_PASSWORD_LENGTH:
        st.error(
            f"`APP_ACCESS_PASSWORD` must be at least {MIN_PASSWORD_LENGTH} characters. "
            "Use a unique, randomly generated password."
        )
        st.stop()

    if st.session_state.get(_AUTHENTICATED_KEY) is True:
        if st.sidebar.button("Sign out", key="vienna_sign_out", use_container_width=True):
            st.session_state.pop(_AUTHENTICATED_KEY, None)
            st.rerun()
        return

    st.title("🔐 Project Vienna")
    st.caption("This dashboard and its crawler controls are restricted.")
    with st.form("vienna_access_form", clear_on_submit=True):
        candidate = st.text_input("Access password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if submitted:
        if password_matches(candidate, configured):
            st.session_state[_AUTHENTICATED_KEY] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()
