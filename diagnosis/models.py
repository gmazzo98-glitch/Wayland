"""
Database tables for the diagnosis layer.

Each ticket that needs storage defines its tables in its OWN module, diagnosis/models_<area>.py, on the shared
`models.Base`; this file only imports them all (registry-style) so that `database.init_db()` creates them.
That keeps models.py (a hot file) and this file out of every ticket's write scope.

Rules for a models_<area>.py:
  * NEW tables only. Never add a column to an existing table: SQLite auto-migrates columns but Postgres (the live
    Neon database) does not, and an unguarded ALTER at app start can stop the app booting.
  * Prefix table names with `dx_` so they are recognisable and cannot collide with the scoring tables.
  * Use JSON columns (models.py imports JSON from sqlalchemy) for blobs; keep a `schema_version` column.
"""

import importlib
import pkgutil

import diagnosis as _pkg

for _m in pkgutil.iter_modules(_pkg.__path__):
    if _m.name.startswith("models_"):
        importlib.import_module(f"diagnosis.{_m.name}")
