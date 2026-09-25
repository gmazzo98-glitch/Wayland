# Vienna Worker Shim

Replaces Supabase's auto-generated PostgREST layer for the crawler-worker RPC calls
(`worker/supabase_rpc.sql`), now that the app's database lives on Neon instead of Supabase.
It's a thin HTTP-to-SQL translator: friend-PC workers (`worker/worker.mjs`) call it instead
of a Postgres REST API, and it forwards the call to the same token-checked
`vienna_worker_*` SQL functions. It holds the real `DATABASE_URL`; workers never do.

## Local run

```
pip install -r requirements.txt
DATABASE_URL=<neon-pooled-connection-string> uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Deploying

Deployed on Vercel (project `vienna-worker-shim`, team `agri-portal`), linked to this
repo's `worker_shim/` directory — pushes to `master` auto-deploy. `DATABASE_URL` is set as
an encrypted production env var there. `vercel.json` sets `maxDuration: 30` on the
function since a Neon cold connection plus a crawl claim can take a few seconds.

Alternatives if Vercel doesn't fit (e.g. serverless cold starts prove too slow for the
15s heartbeat cadence):

- **Render** (free web service) — new Web Service, root dir `worker_shim/`, build
  `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
  env var `DATABASE_URL`. Free tier sleeps after 15 min idle, but the worker's own 15s
  heartbeat keeps it warm while any worker is running.
- **Fly.io** (free allowance) — `fly launch` in this directory, `fly secrets set
  DATABASE_URL=...`, `fly deploy`.
- Any small VPS — same two commands as the local run, behind a process manager.

`WORKER_SHIM_URL` in the main app's `.env` (or Streamlit secrets) must point at whichever
one is live. The Crawler Setup page won't hand out worker installers until that's set.

## Rolling out to already-installed workers

Existing worker installs on friends' PCs still have the old Supabase URL/key baked into
their `worker.config.json`. They'll fail (visible as 🔴 in Crawler Setup) until
re-installed — there's no remote config push. Either wait for the "next add a computer"
flow to naturally replace them, or ask each helper to re-run the setup file.
