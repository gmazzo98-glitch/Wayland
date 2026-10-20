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

-- The site evidence tables (T26) are read and written only through the functions below and by the app itself.
do $rls$
declare
  t text;
begin
  foreach t in array array['site_evidence_runs', 'site_evidence_events', 'site_evidence_companies'] loop
    if to_regclass(t) is not null then
      execute format('alter table %I enable row level security', t);
    end if;
  end loop;
end
$rls$;

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

-- ---- site evidence reader (T26): the worker reports a run's progress, audit events and results ----------
--
-- One call carries a batch from the worker's local outbox: the run's latest progress, audit events,
-- and per-company outcomes (with the signals the tool already derived). Everything is idempotent, so a
-- batch that is sent twice (a lost response, a re-sync from the worker's local files) changes nothing:
--   * events are inserted with ON CONFLICT DO NOTHING on their writer-chosen id;
--   * a company's outcome row is overwritten only by a result that is not older;
--   * a signal is created or refreshed ONLY if no other source holds a real value for it.
-- Each company is its own sub-transaction, so one bad record (unknown company, a constraint, a
-- malformed value) is reported in `errors` and audited, and never blocks the rest of the batch.
-- This function only ever writes the five existing indicators listed below (no new keys, ground rule 1).

create or replace function vienna_worker_site_evidence_report(p_token text, p_report jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
  w crawler_workers%rowtype;
  r site_evidence_runs%rowtype;
  ex signal_records%rowtype;
  v_allowed constant text[] := array['university_partnership', 'external_collaboration',
    'prior_open_innovation_usage', 'international_sales_volume', 'years_international_activity'];
  v_min_rules constant integer := 4;
  v_source constant text := 'Site Evidence Reader';
  v_now timestamp := (now() at time zone 'utc');
  v_run_id text;
  v_run jsonb;
  v_status text;
  e jsonb;
  c jsonb;
  s jsonb;
  rec jsonb;
  v_company_id text;
  v_reg text;
  v_key text;
  v_action text;
  v_reason text;
  v_value double precision;
  v_conf double precision;
  v_rules integer;
  v_finished timestamp;
  v_payload text;
  v_prev jsonb;
  v_has_blob boolean;
  n_events integer := 0;
  n_companies integer := 0;
  errors jsonb := '[]'::jsonb;
begin
  select * into w from crawler_workers
   where token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  if not found or w.revoked then
    return jsonb_build_object('ok', false, 'reason', 'unknown_or_revoked');
  end if;
  update crawler_workers set last_seen_at = v_now where id = w.id;

  if p_report is null or jsonb_typeof(p_report) <> 'object' then
    return jsonb_build_object('ok', false, 'reason', 'bad_report');
  end if;
  v_run_id := left(coalesce(p_report->>'run_id', ''), 36);
  if v_run_id = '' then
    return jsonb_build_object('ok', false, 'reason', 'missing_run_id');
  end if;

  select * into r from site_evidence_runs where id = v_run_id;
  if found and r.worker_id <> w.id then
    return jsonb_build_object('ok', false, 'reason', 'run_belongs_to_another_worker');
  end if;
  if not found then
    insert into site_evidence_runs (id, worker_id, status, requested_at, signals_written, signals_skipped)
    values (v_run_id, w.id, 'running', v_now, 0, 0);
  end if;

  -- ---- the run itself ------------------------------------------------------------------------------
  v_run := case when jsonb_typeof(p_report->'run') = 'object' then p_report->'run' else '{}'::jsonb end;
  v_status := v_run->>'status';
  if v_status is not null and v_status not in ('queued', 'running', 'stopping', 'finished', 'stopped', 'failed') then
    v_status := null;
  end if;
  -- An ended run stays ended: results of it can still arrive late (a resync, a batch that was in flight), and
  -- those carry "running"; they must never reopen a run that already finished, stopped or failed.
  if r.status in ('finished', 'stopped', 'failed') and v_status in ('queued', 'running', 'stopping') then
    v_status := null;
  end if;
  begin
    update site_evidence_runs set
      status = coalesce(v_status, status),
      started_at = coalesce(started_at, nullif(v_run->>'started_at', '')::timestamptz at time zone 'utc'),
      finished_at = case when coalesce(v_status, status) in ('finished', 'stopped', 'failed')
                         then coalesce(nullif(v_run->>'finished_at', '')::timestamptz at time zone 'utc', finished_at, v_now)
                         else null end,
      last_report_at = v_now,
      task_id = coalesce(nullif(p_report->>'task_id', '')::integer, task_id),
      input_total = coalesce(nullif(v_run->>'total', '')::integer, input_total),
      done = coalesce(nullif(v_run->>'done', '')::integer, done),
      hits_total = coalesce(nullif(v_run->>'hits_total', '')::integer, hits_total),
      summary = case when jsonb_typeof(v_run->'summary') = 'object' then (v_run->'summary')::json else summary end,
      info = case when jsonb_typeof(v_run->'info') = 'object' then (v_run->'info')::json else info end,
      last_error = case when v_run ? 'last_error' then left(v_run->>'last_error', 4000) else last_error end
    where id = v_run_id;
  exception when others then
    errors := errors || jsonb_build_object('scope', 'run', 'error', sqlerrm);
  end;

  -- ---- audit events ---------------------------------------------------------------------------------
  for e in select value from jsonb_array_elements(
      case when jsonb_typeof(p_report->'events') = 'array' then p_report->'events' else '[]'::jsonb end) loop
    begin
      insert into site_evidence_events (id, run_id, ts, source, level, code, message, data)
      values (left(e->>'id', 36), v_run_id,
              coalesce(nullif(e->>'ts', '')::timestamptz at time zone 'utc', v_now),
              left(coalesce(e->>'source', 'worker'), 20),
              case when e->>'level' in ('info', 'warn', 'error') then e->>'level' else 'info' end,
              left(coalesce(e->>'code', 'event'), 60), left(coalesce(e->>'message', ''), 2000),
              case when e->'data' is null or jsonb_typeof(e->'data') = 'null' then null else (e->'data')::json end)
      on conflict (id) do nothing;
      if found then
        n_events := n_events + 1;
      end if;
    exception when others then
      errors := errors || jsonb_build_object('scope', 'event', 'id', e->>'id', 'error', sqlerrm);
    end;
  end loop;

  -- ---- per-company outcomes and the signals they carry -------------------------------------------------
  for c in select value from jsonb_array_elements(
      case when jsonb_typeof(p_report->'companies') = 'array' then p_report->'companies' else '[]'::jsonb end) loop
    v_reg := left(coalesce(c->>'company_id', ''), 100);
    begin
      v_company_id := null;
      select id into v_company_id from companies where registration_number = v_reg;
      if v_company_id is null then
        errors := errors || jsonb_build_object('scope', 'company', 'company_id', v_reg,
                                               'error', 'no company has this registration number');
        insert into site_evidence_events (id, run_id, ts, source, level, code, message, data)
        values (md5(v_run_id || '|unmatched|' || v_reg), v_run_id, v_now, 'database', 'warn', 'company_unmatched',
                'No company in the database has registration number ' || v_reg || '; its result was not stored',
                jsonb_build_object('registration_number', v_reg, 'name', c->>'name')::json)
        on conflict (id) do nothing;
        continue;
      end if;

      rec := case when jsonb_typeof(c->'record') = 'object' then c->'record' else '{}'::jsonb end;
      v_finished := coalesce(nullif(c->>'finished_at', '')::timestamptz at time zone 'utc', v_now);
      v_rules := coalesce(nullif(c->>'rules_version', '')::integer, 1);

      insert into site_evidence_companies (id, company_id, registration_number, status, attempts, pages_read, via,
          seconds, rules_version, hits, windows, rejected, rejected_samples, pages_failed, site_detail, error,
          finished_at, last_run_id, updated_at)
      values (gen_random_uuid()::text, v_company_id, v_reg, left(coalesce(rec->>'status', 'unknown'), 30),
          nullif(rec->>'attempts', '')::integer, nullif(rec->>'pages_read', '')::integer, left(rec->>'via', 10),
          nullif(rec->>'seconds', '')::double precision, v_rules,
          (rec->'hits')::json, (rec->'windows')::json, (rec->'rejected')::json, (rec->'rejected_samples')::json,
          (rec->'pages_failed')::json, left(rec->>'site_detail', 2000), left(rec->>'error', 2000),
          v_finished, v_run_id, v_now)
      on conflict (company_id) do update set
          registration_number = excluded.registration_number, status = excluded.status, attempts = excluded.attempts,
          pages_read = excluded.pages_read, via = excluded.via, seconds = excluded.seconds,
          rules_version = excluded.rules_version, hits = excluded.hits, windows = excluded.windows,
          rejected = excluded.rejected, rejected_samples = excluded.rejected_samples,
          pages_failed = excluded.pages_failed, site_detail = excluded.site_detail, error = excluded.error,
          finished_at = excluded.finished_at, last_run_id = excluded.last_run_id, updated_at = excluded.updated_at
        where site_evidence_companies.finished_at is null or excluded.finished_at >= site_evidence_companies.finished_at;
      n_companies := n_companies + 1;

      v_has_blob := false;
      for s in select value from jsonb_array_elements(
          case when jsonb_typeof(c->'signals') = 'array' then c->'signals' else '[]'::jsonb end) loop
        v_key := s->>'signal_key';
        v_action := null;
        v_reason := null;
        ex := null;

        if v_rules < v_min_rules then
          v_action := 'skipped';
          v_reason := 'judged under rules version ' || v_rules || ', older than the required ' || v_min_rules;
        elsif v_key is null or not (v_key = any (v_allowed)) then
          v_action := 'skipped';
          v_reason := 'not one of the indicators this producer may write: ' || coalesce(v_key, 'null');
        else
          v_value := nullif(s->>'value', '')::double precision;
          v_conf := least(1.0, greatest(0.0, coalesce(nullif(s->>'confidence', '')::double precision, 0.5)));
          if v_value is null or v_value < 0 or v_value > 1000 then
            v_action := 'skipped';
            v_reason := 'value missing or out of range: ' || coalesce(s->>'value', 'null');
          else
            select * into ex from signal_records
             where company_id = v_company_id and signal_key = v_key
             order by fetched_at desc nulls last limit 1;
            if not found then
              v_action := 'created';
            elsif ex.source = v_source then
              v_action := 'updated';
            elsif ex.is_simulated or ex.status = 'not_yet_checked' then
              v_action := 'replaced';
            else
              v_action := 'skipped';
              v_reason := case when ex.status = 'absent'
                               then ex.source || ' recorded this as absent: a conflict for a human to settle'
                               else 'already supplied by ' || ex.source end;
            end if;
          end if;
        end if;

        v_prev := null;
        if ex.id is not null then
          v_prev := jsonb_build_object('id', ex.id, 'source', ex.source, 'status', ex.status,
                      'numeric_value', ex.numeric_value, 'text_value', ex.text_value, 'confidence', ex.confidence,
                      'is_simulated', ex.is_simulated, 'fetched_at', ex.fetched_at,
                      'raw_payload_ref', ex.raw_payload_ref);
        end if;

        if v_action in ('created', 'updated', 'replaced') then
          v_payload := jsonb_build_object('dataset', 'crawler_site_evidence', 'simulated', false, 'run_id', v_run_id,
                                          'evidence', s->'evidence')::text;
          if v_action = 'created' then
            insert into signal_records (id, company_id, signal_key, source, numeric_value, text_value, status,
                                        confidence, is_simulated, fetched_at, raw_payload_ref)
            values (gen_random_uuid()::text, v_company_id, v_key, v_source, v_value, left(s->>'summary', 4000),
                    'present', v_conf, false, v_now, v_payload);
          else
            update signal_records set source = v_source, numeric_value = v_value, text_value = left(s->>'summary', 4000),
                   status = 'present', confidence = v_conf, is_simulated = false, fetched_at = v_now,
                   raw_payload_ref = v_payload
             where id = ex.id;
          end if;
          v_has_blob := true;
        end if;

        insert into site_evidence_events (id, run_id, ts, source, level, code, message, data)
        -- No action in the id: a replayed batch finds the signal already there ("updated" instead of
        -- "created") and must still collide with the original event, which also holds the original `previous`.
        values (md5(v_run_id || '|' || v_reg || '|' || coalesce(v_key, '') || '|' ||
                    to_char(v_finished, 'YYYYMMDDHH24MISSUS')),
                v_run_id, v_now, 'database', case when v_action = 'skipped' then 'warn' else 'info' end,
                'signal_' || v_action,
                case when v_action = 'skipped'
                     then 'Left ' || coalesce(v_key, '?') || ' alone for ' || v_reg || ': ' || v_reason
                     else 'Wrote ' || v_key || ' = ' || v_value || ' for ' || v_reg || ' (' || v_action || ')' end,
                jsonb_build_object('company_id', v_company_id, 'registration_number', v_reg, 'name', c->>'name',
                                   'signal_key', v_key, 'action', v_action, 'value', v_value, 'reason', v_reason,
                                   'previous', v_prev, 'summary', left(s->>'summary', 400))::json)
        on conflict (id) do nothing;
      end loop;

      if (jsonb_typeof(c->'hits') = 'array' and jsonb_array_length(c->'hits') > 0) then
        insert into raw_import_records (id, company_id, dataset_name, source_filename, raw_row, imported_at, updated_at)
        values (gen_random_uuid()::text, v_company_id, 'crawler_site_evidence', 'crawler:crawler_site_evidence',
                jsonb_build_object('hits', c->'hits', 'coverage', rec, 'run_id', v_run_id,
                                   'imported_at', to_char(v_now, 'YYYY-MM-DD"T"HH24:MI:SS'))::json, v_now, v_now)
        on conflict (company_id, dataset_name) do update set raw_row = excluded.raw_row, updated_at = excluded.updated_at;
      end if;
    exception when others then
      errors := errors || jsonb_build_object('scope', 'company', 'company_id', v_reg, 'error', sqlerrm);
      begin
        insert into site_evidence_events (id, run_id, ts, source, level, code, message, data)
        values (md5(v_run_id || '|failed|' || v_reg || '|' || coalesce(c->>'finished_at', '')), v_run_id, v_now,
                'database', 'error', 'company_report_failed',
                'The database refused the result for ' || v_reg || ': ' || sqlerrm,
                jsonb_build_object('registration_number', v_reg, 'name', c->>'name', 'sqlstate', sqlstate,
                                   'error', sqlerrm)::json)
        on conflict (id) do nothing;
      exception when others then
        null;
      end;
    end;
  end loop;

  -- ---- counters, derived from the audit trail so a re-sent batch cannot inflate them -----------------
  begin
    update site_evidence_runs set
      signals_written = (select count(*) from site_evidence_events
                          where run_id = v_run_id and code in ('signal_created', 'signal_updated', 'signal_replaced')),
      signals_skipped = (select count(*) from site_evidence_events where run_id = v_run_id and code = 'signal_skipped')
    where id = v_run_id;

    if n_companies > 0 or jsonb_array_length(errors) > 0 then
      insert into source_health (source_name, phase, last_run_at, last_status, mode, total_calls, total_cost, error_count,
                                 last_error_message)
      values (v_source, 7, v_now, case when jsonb_array_length(errors) > 0 then 'error' else 'success' end, 'live',
              n_companies, 0, jsonb_array_length(errors), errors->0->>'error')
      on conflict (source_name) do update set
        last_run_at = excluded.last_run_at, last_status = excluded.last_status, mode = 'live',
        total_calls = source_health.total_calls + n_companies,
        error_count = source_health.error_count + jsonb_array_length(errors),
        last_error_message = coalesce(excluded.last_error_message, source_health.last_error_message);
    end if;
  exception when others then
    errors := errors || jsonb_build_object('scope', 'counters', 'error', sqlerrm);
  end;

  return jsonb_build_object('ok', true, 'run_id', v_run_id, 'events', n_events, 'companies', n_companies,
    'errors', (select coalesce(jsonb_agg(x), '[]'::jsonb) from (select x from jsonb_array_elements(errors) x limit 20) t),
    'error_count', jsonb_array_length(errors));
end
$fn$;

do $grants$
begin
  revoke all on function vienna_worker_heartbeat(text, jsonb) from public;
  revoke all on function vienna_worker_claim(text) from public;
  revoke all on function vienna_worker_complete(text, integer, boolean, jsonb, text) from public;
  revoke all on function vienna_worker_site_evidence_report(text, jsonb) from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function vienna_worker_heartbeat(text, jsonb) to anon;
    grant execute on function vienna_worker_claim(text) to anon;
    grant execute on function vienna_worker_complete(text, integer, boolean, jsonb, text) to anon;
    grant execute on function vienna_worker_site_evidence_report(text, jsonb) to anon;
    revoke all on table crawler_workers from anon;
    revoke all on table crawler_tasks from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table crawler_workers from authenticated;
    revoke all on table crawler_tasks from authenticated;
  end if;
end
$grants$;
