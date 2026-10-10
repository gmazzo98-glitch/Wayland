#!/usr/bin/env python3
"""One-time Gmail OAuth setup for the Wayland Morning Journal.

Runs a tiny local server to complete Google's OAuth consent flow and prints
a REFRESH TOKEN that you then store as the GitHub Actions secret
GMAIL_REFRESH_TOKEN. Pure standard library — no packages to install.

Prerequisite (once, in Google Cloud Console — free):
  1. console.cloud.google.com → create (or pick) a project
  2. "APIs & Services" → "Library" → enable the "Gmail API"
  3. "APIs & Services" → "OAuth consent screen" → External → fill the
     required fields → publish it (Production) so refresh tokens do not
     expire every 7 days → add your Gmail address as a test user
  4. "APIs & Services" → "Credentials" → "Create credentials" →
     "OAuth client ID" → type "Desktop app" → create → copy the
     Client ID and Client secret

Then run:  python gmail_oauth_setup.py
Paste the Client ID and secret when prompted; a browser window opens,
log into gmazzo98@gmail.com, click through the "unverified app" warning
("Advanced" → "Go to app"), and approve. The refresh token is printed
here — save it as the GMAIL_REFRESH_TOKEN secret.
"""
from __future__ import annotations

import http.server
import json
import queue
import urllib.parse
import urllib.request
import webbrowser

PORT = 8090
SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = input("Client secret: ").strip()

    codes: "queue.Queue[str]" = queue.Queue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            if code:
                codes.put(code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Authorization received — you can close this tab.</h2>")
            else:
                detail = error or "unknown error"
                codes.put(f"ERROR:{detail}")
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h2>Authorization failed: {detail}</h2>".encode())

        def log_message(self, *args):
            pass

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{PORT}/",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })

    print("Opening the browser for Google sign-in...")
    webbrowser.open(auth_url)

    with http.server.HTTPServer(("localhost", PORT), Handler) as server:
        print(f"Waiting for the authorization callback on http://localhost:{PORT}/ ...")
        server.handle_request()

    code = codes.get()
    if code.startswith("ERROR:"):
        raise SystemExit(f"Authorization failed: {code[len('ERROR:'):]}. Run the script again.")

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"http://localhost:{PORT}/",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tokens = json.loads(r.read().decode("utf-8"))

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit("Google returned no refresh token. Revoke the app at "
                         "https://myaccount.google.com/permissions and run again "
                         "(the prompt=consent step is required).")

    print()
    print("=" * 60)
    print("SUCCESS — store these as GitHub Actions secrets:")
    print(f"  GMAIL_CLIENT_ID     = {client_id}")
    print(f"  GMAIL_CLIENT_SECRET = {client_secret}")
    print(f"  GMAIL_REFRESH_TOKEN = {refresh_token}")
    print("=" * 60)


if __name__ == "__main__":
    main()
