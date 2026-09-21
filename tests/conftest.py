"""
Test isolation: no test may touch the DATABASE_URL in .env, which is the live Supabase project.

config.py reads DATABASE_URL when it is first imported, and load_dotenv() never overrides a variable
that is already set — so pointing it at a throwaway SQLite file HERE, before any project module is
imported, is what keeps tests/test_company_management.py (its fixture calls init_db(), which creates
tables/columns and seeds rows) off production. Tests that build their own engine (tmp_path files,
sqlite:///:memory:) never read DATABASE_URL and are unaffected.

Opt out only for a deliberate run against the real database: VIENNA_TESTS_ALLOW_LIVE_DB=1. It is never
set by default, and the run says so loudly.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ALLOW_LIVE_DB_ENV = "VIENNA_TESTS_ALLOW_LIVE_DB"

# config/database live in the project root, and this conftest imports config itself below, so it must not
# depend on how pytest was launched (`python -m pytest` puts the cwd on sys.path; a bare `pytest` doesn't).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_ALLOW_LIVE_DB = os.environ.get(ALLOW_LIVE_DB_ENV, "").strip().lower() in ("1", "true", "yes")
_TEST_DB_DIR = None

if not _ALLOW_LIVE_DB:
    # mkdtemp rather than tmp_path_factory: this runs at conftest import, before any fixture exists.
    _TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="vienna_tests_"))
    # Overrides a DATABASE_URL already exported in the shell too — the point is that nothing can reach prod.
    os.environ["DATABASE_URL"] = "sqlite:///" + (_TEST_DB_DIR / "vienna_tests.db").as_posix()

import config as app_config  # noqa: E402 — must come after DATABASE_URL is set

if not _ALLOW_LIVE_DB and not app_config.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///"):
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)
    pytest.exit(
        "Refusing to run: config.SQLALCHEMY_DATABASE_URI is not a sqlite:/// URL, so the suite could reach "
        "the live database. config was probably imported before tests/conftest.py (e.g. another path was "
        "collected first). Run `python -m pytest tests`, or set %s=1 to knowingly use DATABASE_URL as-is."
        % ALLOW_LIVE_DB_ENV,
        returncode=2,
    )


def _live_db_warning() -> str:
    from sqlalchemy.engine import make_url
    target = make_url(app_config.SQLALCHEMY_DATABASE_URI)
    where = target.host or target.database or "?"  # never the password
    return (
        f"{ALLOW_LIVE_DB_ENV} is set: tests will run against DATABASE_URL as-is ({target.drivername}://{where}). "
        "test_company_management.py calls init_db() and writes/deletes rows there."
    )


def pytest_report_header():
    if _ALLOW_LIVE_DB:
        return "vienna: WARNING — " + _live_db_warning()
    return f"vienna: DATABASE_URL isolated -> {app_config.SQLALCHEMY_DATABASE_URI}"


def pytest_sessionstart(session):
    # The report header is hidden by -q, and this must not be missed.
    if _ALLOW_LIVE_DB:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("WARNING: " + _live_db_warning(), red=True, bold=True)


def pytest_unconfigure():
    if _TEST_DB_DIR is None:
        return
    database = sys.modules.get("database")
    if database is not None:
        database.engine.dispose()  # Windows won't delete an SQLite file that still has a pooled connection
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)
