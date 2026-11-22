"""Request a deployment of the Vienna worker fleet mode from Crawler Setup."""

import base64
import json
import re

import requests

REPO_URL = "https://github.com/gmazzo98-glitch/Wayland"
BRANCH = "master"
MODE_PATH = "worker_shim/app/fleet_mode.json"
EDIT_URL = f"{REPO_URL}/edit/{BRANCH}/{MODE_PATH}"
_API_URL = f"https://api.github.com/repos/gmazzo98-glitch/Wayland/contents/{MODE_PATH}"


class FleetControlError(Exception):
    """A safe, operator-facing error with no response body or credentials."""


def request_fleet_mode(paused: bool, token: str) -> None:
    """Commit only the fleet flag, using GitHub's current blob SHA as a write guard."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    options = {"headers": headers, "timeout": 8, "allow_redirects": False}
    try:
        current = requests.get(_API_URL, params={"ref": BRANCH}, **options)
        if current.status_code != 200:
            raise FleetControlError("GitHub could not read the fleet setting. Check the app's GitHub authorization.")
        entry = current.json()
        sha = entry["sha"]
        if entry["encoding"] != "base64" or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("invalid GitHub file metadata")
        encoded = "".join(entry["content"].split())
        mode = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(mode.get("paused"), bool):
            raise ValueError("invalid fleet setting")

        if mode["paused"] == paused:
            return
        mode["paused"] = paused
        content = base64.b64encode((json.dumps(mode, indent=2) + "\n").encode()).decode()
        result = requests.put(_API_URL, json={
            "message": "Pause Vienna workers" if paused else "Resume Vienna workers",
            "content": content,
            "sha": sha,
            "branch": BRANCH,
        }, **options)
    except requests.RequestException:
        raise FleetControlError("GitHub did not confirm the request. Check the live status before retrying.") from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise FleetControlError("GitHub returned an invalid fleet setting.") from None

    if result.status_code == 409:
        raise FleetControlError("The fleet setting changed during the request. Check the live status and try again.")
    if result.status_code != 200:
        raise FleetControlError("GitHub could not save the fleet setting. Check the app's write permission.")
