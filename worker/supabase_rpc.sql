-- Crawler-worker RPC layer (applied idempotently at app start by worker_hub.ensure_rpc).
--
-- The worker on a helper's computer must NOT hold a database password. It only holds the
-- project's public (publishable) API key plus its own random token, and talks to the
-- database exclusively through the three SECURITY DEFINER functions below. Each one
-- looks the token up (by SHA-256 hash), so a worker can only ever see and complete tasks
-- addressed to itself. The tables themselves have row-level security switched on with
-- no policies, so the public key can read and write nothing directly.

alter table crawler_workers enable row level security;
alter table crawler_tasks   enable row level security;

create or replace function vienna_worker_heartbeat(p_token text, p_info jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
  w crawler_workers%rowtype;
begin
  select * into w from crawler_workers
   where token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  if not found or w.revoked then
    return jsonb_build_object('ok', false, 'reason', 'unknown_or_revoked');
  end if;
  update crawler_workers
     set last_seen_at = (now() at time zone 'utc'), info = p_info::json
   where id = w.id;
  return jsonb_build_object('ok', true, 'worker_id', w.id, 'name', w.name);
end
$fn$;

create or replace function vienna_worker_claim(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
  w crawler_workers%rowtype;
  t crawler_tasks%rowtype;
begin
  select * into w from crawler_workers
   where token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  if not found or w.revoked then
    return jsonb_build_object('ok', false, 'reason', 'unknown_or_revoked');
  end if;
  update crawler_workers set last_seen_at = (now() at time zone 'utc') where id = w.id;

  select * into t from crawler_tasks
   where worker_id = w.id and status = 'queued'
   order by id
   for update skip locked
   limit 1;
  if not found then
    return jsonb_build_object('ok', true, 'task', null);
  end if;

  update crawler_tasks
     set status = 'running', claimed_at = (now() at time zone 'utc')
   where id = t.id;
  return jsonb_build_object(
    'ok', true,
    'task', jsonb_build_object('id', t.id, 'crawler', t.crawler, 'kind', t.kind, 'request', t.request::jsonb)
  );
end
$fn$;

create or replace function vienna_worker_complete(
  p_token text, p_task_id integer, p_ok boolean, p_result jsonb, p_error text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
  w crawler_workers%rowtype;
  n integer;
begin
  select * into w from crawler_workers
   where token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  if not found or w.revoked then
    return jsonb_build_object('ok', false, 'reason', 'unknown_or_revoked');
  end if;

  -- The request carried API keys for this one run; drop them the moment it is finished.
  update crawler_tasks
     set status = case when p_ok then 'done' else 'error' end,
         result = p_result::json,
         error = left(p_error, 4000),
         finished_at = (now() at time zone 'utc'),
         request = ((request::jsonb) - 'env')::json
   where id = p_task_id and worker_id = w.id and status = 'running';
  get diagnostics n = row_count;
  return jsonb_build_object('ok', true, 'accepted', n > 0);
end
$fn$;

do $grants$
begin
  revoke all on function vienna_worker_heartbeat(text, jsonb) from public;
  revoke all on function vienna_worker_claim(text) from public;
  revoke all on function vienna_worker_complete(text, integer, boolean, jsonb, text) from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function vienna_worker_heartbeat(text, jsonb) to anon;
    grant execute on function vienna_worker_claim(text) to anon;
    grant execute on function vienna_worker_complete(text, integer, boolean, jsonb, text) to anon;
    revoke all on table crawler_workers from anon;
    revoke all on table crawler_tasks from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table crawler_workers from authenticated;
    revoke all on table crawler_tasks from authenticated;
  end if;
end
$grants$;
