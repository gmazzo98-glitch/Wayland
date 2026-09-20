"""
Builds the file a helper downloads from the Crawler Setup page: ONE self-contained Windows
setup file (.bat) that they double-click. Nothing from the Vienna app is in it — only the
crawler bundle (scripts/build_worker_bundle.py), the installer script, and this one
computer's connection details (the public Supabase URL + key, and its own random token).

Why a single .bat and not a zip to unpack first: a zip opened straight from the browser or
Explorer runs its files from a temporary folder with their neighbours missing, which is
exactly the kind of thing that makes an installer "not work" for a non-technical person. A
setup file that carries its own payload has nothing to go missing. The .bat is only a stub:
it lifts the PowerShell installer out of itself and runs it; the crawler bundle (a zip, base64
encoded) rides at the end of the same file and is unpacked by that installer.

File layout, after the stub:
    ##PS1            <- the PowerShell installer (worker/install.ps1)
    ##ZIP            <- the bundle zip + worker.config.json, base64
"""

import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
BUNDLE_PATH = ROOT / "worker_dist" / "vienna-crawler-bundle.zip"
INSTALL_PS1 = ROOT / "worker" / "install.ps1"

SETUP_BAT_NAME = "Vienna-Crawler-Setup.bat"
SETUP_ZIP_NAME = "Vienna-Crawler-Setup.zip"

# The stub. Its PowerShell one-liner has no double quotes and never spells a marker out in one
# piece ('##'+'PS1'), so it can't find itself instead of the real marker lines below it.
_STUB = r"""@echo off
setlocal
title Vienna Crawler Setup
set "VIENNA_SETUP_SOURCE=%~f0"
echo.
echo   Starting the Vienna Crawler Setup. Please wait a moment...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:VIENNA_SETUP_SOURCE); $n=[string][char]10; $a=$t.IndexOf($n+'##'+'PS1'); $b=$t.IndexOf($n+'##'+'ZIP'); [IO.File]::WriteAllText($env:TEMP+'\vienna-setup.ps1',$t.Substring($a+6,$b-$a-6),[Text.Encoding]::ASCII)"
if not exist "%TEMP%\vienna-setup.ps1" (
  echo.
  echo   Setup could not start. Please download the setup file again.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\vienna-setup.ps1" %*
set "RC=%ERRORLEVEL%"
del "%TEMP%\vienna-setup.ps1" >nul 2>&1
exit /b %RC%
"""


def bundle_available() -> bool:
    return BUNDLE_PATH.is_file()


_info_cache: Dict[str, Any] = {}


def bundle_info() -> Optional[Dict[str, Any]]:
    """The bundle's own VERSION.json (version, build id, crawler list), or None when the
    zip has not been built. Cached per file modification time."""
    if not bundle_available():
        return None
    stamp = BUNDLE_PATH.stat().st_mtime
    if _info_cache.get("stamp") != stamp:
        with zipfile.ZipFile(BUNDLE_PATH) as z:
            _info_cache["info"] = json.loads(z.read("VERSION.json").decode("utf-8"))
        _info_cache["stamp"] = stamp
    return _info_cache["info"]


def personalized_bundle(config: Dict[str, Any]) -> bytes:
    """The bundle zip plus this computer's worker.config.json."""
    if not bundle_available():
        raise FileNotFoundError(
            "worker_dist/vienna-crawler-bundle.zip is missing — run "
            "`python scripts/build_worker_bundle.py` and commit the result.")
    buf = io.BytesIO(BUNDLE_PATH.read_bytes())
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("worker.config.json", json.dumps(config, indent=2))
    return buf.getvalue()


def _crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def build_setup_bat(config: Dict[str, Any]) -> bytes:
    payload = base64.b64encode(personalized_bundle(config)).decode("ascii")
    wrapped = "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
    text = _crlf(_STUB) + "\r\n##PS1\r\n" + _crlf(INSTALL_PS1.read_text(encoding="utf-8")) + "\r\n##ZIP\r\n" + _crlf(wrapped) + "\r\n"
    return text.encode("ascii")  # any stray non-ASCII character in the installer is a bug: fail loudly


def build_setup_zip(config: Dict[str, Any]) -> bytes:
    """The same setup file inside a zip — for browsers/antivirus that refuse to download a bare .bat."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(SETUP_BAT_NAME, build_setup_bat(config))
    return buf.getvalue()


def make_config(worker_name: str, token: str, supabase_url: str, anon_key: str, max_parallel: int = 3) -> Dict[str, Any]:
    return {"name": worker_name, "token": token, "supabaseUrl": supabase_url, "anonKey": anon_key,
            "maxParallel": max_parallel}
