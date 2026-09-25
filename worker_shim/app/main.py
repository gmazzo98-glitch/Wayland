"""Vienna Worker Shim — replaces Supabase's auto-generated PostgREST layer.

Friend-PC crawler workers (worker/worker.mjs) never hold a database password; they only
know this shim's URL plus their own random token. This service holds the real Postgres
credentials and does nothing but forward the 3 whitelisted calls to the SECURITY DEFINER
functions in worker/supabase_rpc.sql, which do the actual token check. So this shim is not
a new trust boundary — it's a dumb HTTP-to-SQL translator, exactly what PostgREST was doing.

Run locally: uvicorn app.main:app --host 0.0.0.0 --port 8000
Needs: DATABASE_URL (the same Postgres connection string the main app uses)

The connection pool is built at import time rather than through an ASGI lifespan hook,
since serverless Python runtimes (Vercel's included) don't reliably fire lifespan events —
this way it works the same whether the module is loaded by uvicorn or by a serverless
cold start.
"""

import os

import psycopg2
from fastapi import FastAPI, HTTPException, Request
from psycopg2.extras import Json
from psycopg2.pool import SimpleConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]

# Each entry: ordered (arg_name, postgres_type) pairs matching the function's signature
# in worker/supabase_rpc.sql exactly. The type cast keeps psycopg2 from guessing wrong
# (e.g. a Python dict must go through Json() and be cast to jsonb explicitly).
FUNCTIONS = {
    "vienna_worker_heartbeat": [("p_token", "text"), ("p_info", "jsonb")],
    "vienna_worker_claim": [("p_token", "text")],
    "vienna_worker_complete": [
        ("p_token", "text"), ("p_task_id", "integer"), ("p_ok", "boolean"),
        ("p_result", "jsonb"), ("p_error", "text"),
    ],
}

pool = SimpleConnectionPool(1, 10, DATABASE_URL)

app = FastAPI()


@app.post("/rpc/{fn}")
async def rpc(fn: str, req: Request):
    spec = FUNCTIONS.get(fn)
    if spec is None:
        raise HTTPException(404, "unknown function")

    body = await req.json()
    call_args = []
    params = {}
    for name, pg_type in spec:
        if name not in body:
            raise HTTPException(400, f"missing argument {name}")
        value = body[name]
        params[name] = Json(value) if pg_type == "jsonb" and value is not None else value
        call_args.append(f"{name} => %({name})s::{pg_type}")

    query = f"select {fn}({', '.join(call_args)})"

    # A pooled connection that's been idle (cold start, or Neon's own pooler recycling it)
    # can come back dead; one retry on a fresh connection covers that without the caller
    # (a crawler worker mid-run) ever seeing it.
    last_error = None
    for attempt in (1, 2):
        conn = pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    (result,) = cur.fetchone()
            pool.putconn(conn)
            return result
        except psycopg2.OperationalError as e:
            last_error = e
            pool.putconn(conn, close=True)
    raise last_error


@app.get("/healthz")
async def healthz():
    return {"ok": True}
