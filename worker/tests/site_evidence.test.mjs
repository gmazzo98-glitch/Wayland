// Tests for worker/site_evidence.mjs. Run with:  node --test worker/tests/
// No network, no database, no Python: the clock, the process table and the server are fakes; the folder is real.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  createSiteEvidence, KEEPER_MS, MAX_RESTARTS_IN_WINDOW, PROGRESS_REPORT_MS, SPAWN_GRACE_MS, UPDATE_DEFER_MS, readLinesFrom,
} from '../site_evidence.mjs';

const RUN = '11111111-1111-1111-1111-111111111111';
const RUN2 = '22222222-2222-2222-2222-222222222222';
const CSV = 'company_id,homepage_url,name\nIT001,https://a.it/,A SRL\nIT002,https://b.it/,B SRL\n';

function makeEnv(opts = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'se-root-'));
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'se-out-'));
  fs.mkdirSync(path.join(root, 'tools', 'site_evidence'), { recursive: true });
  if (!opts.noTool) fs.writeFileSync(path.join(root, 'tools', 'site_evidence', 'runner.py'), '# stub');
  let clock = Date.parse('2026-10-20T10:00:00Z');
  const calls = [];
  const spawned = [];
  const alive = new Set();
  const killed = [];
  const env = {
    calls, spawned, alive, killed, root, outDir,
    advance(ms) { clock += ms; },
    get now() { return clock; },
    rpcResult: opts.rpcResult || (() => ({ ok: true, error_count: 0, errors: [] })),
  };
  env.se = createSiteEvidence({
    root, outDir, token: 'tok', log: () => {},
    now: () => clock,
    sleep: async (ms) => { clock += ms; if (opts.exitOnStop && fs.existsSync(path.join(outDir, 'STOP'))) alive.clear(); },
    rpc: async (fn, args) => {
      calls.push({ fn, args });
      const r = env.rpcResult(args, calls.length);
      if (r instanceof Error) throw r;
      return r;
    },
    spawnSyncProcess: (cmd, args) => {
      if (args.includes('--version')) return opts.python || { status: 0, stdout: 'Python 3.12.0', stderr: '' };
      if (args.includes('tools.site_evidence.diagnostics')) {
        return { status: 0, stdout: JSON.stringify({ environment: { tool_build: 'abc' }, problem_companies: [{ company_id: 'IT001' }],
          events: { recent_warnings_and_errors: [] }, log_tails: { 'run.log': ['x'], 'supervisor.log': ['y'] } }), stderr: '' };
      }
      return { status: 1, stdout: '', stderr: 'unexpected' };
    },
    spawnProcess: (cmd, args, o) => {
      const pid = 5000 + spawned.length;
      spawned.push({ cmd, args, o, pid });
      alive.add(pid);
      fs.writeFileSync(path.join(outDir, 'supervisor.pid'), String(pid)); // the real supervisor writes this itself
      return { pid, unref() {} };
    },
    pidAlive: (pid) => alive.has(pid),
    killTree: (pid) => { if (!pid) return; killed.push(pid); alive.delete(pid); },
    probeOllama: async () => opts.ollama || { installed: true, reachable: true, models: ['qwen2.5:3b'] },
    workerInfo: () => ({ build: 'wb1', version: '1.0.0', host: 'PC' }),
  });
  env.P = env.se.paths;
  env.read = (f) => JSON.parse(fs.readFileSync(f, 'utf8'));
  env.cursor = () => (fs.existsSync(env.P.cursor) ? env.read(env.P.cursor) : {});
  env.lines = (f) => (fs.existsSync(f) ? fs.readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
  env.die = (pid = [...alive][0]) => { alive.delete(pid); };
  env.writeExit = (code, reason, run = RUN) => fs.writeFileSync(env.P.exit, JSON.stringify({ code, reason, run_id: run }));
  env.addEvent = (n, run = RUN, extra = {}) => fs.appendFileSync(env.P.events, JSON.stringify({
    id: `e${n}`.padStart(32, '0'), ts: new Date(clock).toISOString(), source: 'runner', run_id: run, level: 'info', code: 'x', message: `m${n}`, data: {}, ...extra }) + '\n');
  env.addCompany = (reg, run = RUN, extra = {}) => fs.appendFileSync(env.P.outbox, JSON.stringify({
    v: 1, run_id: run, company_id: reg, name: `N ${reg}`, finished_at: new Date(clock).toISOString(), rules_version: 4,
    record: { company_id: reg, status: 'done' }, hits: [], signals: [], ...extra }) + '\n');
  return env;
}

const startTask = (id = 1, run = RUN, extra = {}) => ({ id, kind: 'start', request: { run_id: run, input_csv: CSV, options: {}, ...extra } });

// ---- capability --------------------------------------------------------------------------------------------
test('a computer with Python, Ollama and the model can start; every missing piece is named', async () => {
  assert.deepEqual((await makeEnv().se.capability(true)).blockers, []);
  assert.ok((await makeEnv({ python: { status: 1, stdout: '', stderr: '' } }).se.capability(true)).blockers[0].includes('Python'));
  assert.ok((await makeEnv({ ollama: { installed: false, reachable: false, models: [] } }).se.capability(true)).blockers[0].includes('Ollama'));
  const noModel = await makeEnv({ ollama: { installed: true, reachable: true, models: ['llama3:8b'] } }).se.capability(true);
  assert.ok(noModel.blockers[0].includes('qwen2.5:3b') && noModel.blockers[0].includes('llama3:8b'));
  const stopped = await makeEnv({ ollama: { installed: true, reachable: false, models: [] } }).se.capability(true);
  assert.equal(stopped.can_start, true, 'Ollama installed but not running is fine: the tool starts it');
  assert.equal((await makeEnv({ noTool: true }).se.capability(true)).available, false);
});

// ---- start / stop ------------------------------------------------------------------------------------------
test('start writes the company list and control file, launches the supervisor with the run id and logs it', async () => {
  const e = makeEnv();
  const r = await e.se.handleTask(startTask(7));
  assert.deepEqual(r, { run_id: RUN, started: true, total: 2 });
  assert.equal(fs.readFileSync(e.P.companies, 'utf8'), CSV);
  const c = e.read(e.P.control);
  assert.equal(c.run_id, RUN); assert.equal(c.desired, 'running'); assert.equal(c.task_id, 7); assert.equal(c.total, 2);
  assert.equal(e.spawned.length, 1);
  const { cmd, args, o } = e.spawned[0];
  assert.equal(cmd, 'python');
  assert.deepEqual(args.slice(0, 3), ['-u', '-m', 'tools.site_evidence.supervisor']);
  assert.ok(args.includes('--run-id') && args[args.indexOf('--run-id') + 1] === RUN);
  assert.equal(args[args.indexOf('--min-free-gb-to-load') + 1], '1');
  assert.equal(args[args.indexOf('--retry-after-hours') + 1], '6');
  assert.equal(o.cwd, e.root); assert.equal(o.detached, true);
  const codes = e.lines(e.P.events).map((x) => x.code);
  assert.deepEqual(codes, ['run_requested', 'supervisor_launched']);
  assert.equal(e.lines(e.P.events)[0].run_id, RUN);
});

test('start honours the options chosen in the app', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask(1, RUN, { options: { retry_after_hours: 0, min_free_gb_to_load: 2 } }));
  const args = e.spawned[0].args;
  assert.equal(args[args.indexOf('--retry-after-hours') + 1], '0');
  assert.equal(args[args.indexOf('--min-free-gb-to-load') + 1], '2');
});

test('start is refused, and audited, when this computer cannot do it, or the request is malformed, or a run is active', async () => {
  const e = makeEnv({ ollama: { installed: false, reachable: false, models: [] } });
  await assert.rejects(() => e.se.handleTask(startTask()), /Ollama/);
  assert.equal(e.spawned.length, 0);
  assert.equal(e.lines(e.P.events)[0].code, 'start_refused');

  const f = makeEnv();
  await assert.rejects(() => f.se.handleTask({ id: 1, kind: 'start', request: { run_id: 'nope', input_csv: CSV } }), /valid run id/);
  await assert.rejects(() => f.se.handleTask({ id: 1, kind: 'start', request: { run_id: RUN, input_csv: 'company_id,homepage_url,name\n' } }), /company list/);
  await f.se.handleTask(startTask());
  await assert.rejects(() => f.se.handleTask(startTask(2, RUN2)), /already active/);
  assert.deepEqual(await f.se.handleTask(startTask(3, RUN)), { run_id: RUN, already_running: true });
  assert.equal(f.spawned.length, 1);
});

test('stop asks for a clean stop, and the keeper records "stopped" once the supervisor is gone', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  assert.deepEqual(await e.se.handleTask({ id: 2, kind: 'stop' }), { stop_requested: true, run_id: RUN });
  assert.ok(fs.existsSync(e.P.stop)); assert.equal(e.read(e.P.control).state, 'stopping');
  await e.se.keeper();                                     // supervisor still alive: nothing final yet
  assert.equal(e.read(e.P.control).state, 'stopping');
  e.die(); e.writeExit(2, 'stop_file');
  await e.se.keeper();
  const c = e.read(e.P.control);
  assert.equal(c.state, 'stopped'); assert.equal(c.desired, 'stopped'); assert.ok(c.finished_at);
  assert.ok(e.lines(e.P.events).some((x) => x.code === 'run_stopped'));
  assert.equal(e.cursor().pending_finals[0].status, 'stopped');
  assert.deepEqual(await e.se.handleTask({ id: 3, kind: 'stop' }), { stopped: false, reason: 'no active run' });
});

// ---- keeping it alive -----------------------------------------------------------------------------------------
test('a dead supervisor is restarted after the start-up grace, with an audit event that includes the spawn log', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  fs.writeFileSync(e.P.spawnLog, 'Traceback...\nModuleNotFoundError: x\n');
  e.die();                                                  // crashed (or the PC rebooted): no exit record
  await e.se.keeper();
  assert.equal(e.spawned.length, 1, 'inside the grace period a missing process is not yet a crash');
  e.advance(SPAWN_GRACE_MS + 1000);
  await e.se.keeper();
  assert.equal(e.spawned.length, 2);
  const ev = e.lines(e.P.events).find((x) => x.code === 'supervisor_restarted');
  assert.ok(ev && ev.level === 'warn' && ev.data.spawn_log_tail.some((l) => l.includes('ModuleNotFoundError')));
  assert.equal(e.read(e.P.control).restarts.length, 1);
});

test('restarts back off and a crash loop ends the run as FAILED with diagnostics and a final report owed', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  for (let i = 0; i < 12 && e.read(e.P.control).state !== 'failed'; i++) {
    e.die();
    e.advance(Math.max(SPAWN_GRACE_MS + 1000, 15_000 * 2 ** Math.max(0, i - 1) + 1000));   // past the grace and the backoff, no more
    await e.se.keeper();
  }
  const c = e.read(e.P.control);
  assert.equal(c.state, 'failed'); assert.match(c.final_error, /Crash loop/);
  assert.equal(e.spawned.length, 1 + MAX_RESTARTS_IN_WINDOW);
  const codes = e.lines(e.P.events).map((x) => x.code);
  assert.ok(codes.includes('run_failed') && codes.includes('failure_diagnostics'));
  assert.equal(e.cursor().pending_finals[0].status, 'failed');
  const diag = e.lines(e.P.events).find((x) => x.code === 'failure_diagnostics');
  assert.equal(diag.level, 'error'); assert.deepEqual(diag.data.problem_companies, [{ company_id: 'IT001' }]);
});

test('a supervisor that ends by finishing marks the run finished; fatal configuration marks it failed without restarting', async () => {
  const a = makeEnv();
  await a.se.handleTask(startTask());
  a.die(); a.writeExit(0, 'finished');
  await a.se.keeper();
  assert.equal(a.read(a.P.control).state, 'finished');
  assert.equal(a.spawned.length, 1);
  assert.ok(a.lines(a.P.events).some((x) => x.code === 'run_finished'));

  const b = makeEnv();
  await b.se.handleTask(startTask());
  b.die(); b.writeExit(3, 'fatal_configuration');
  await b.se.keeper();
  assert.equal(b.read(b.P.control).state, 'failed');
  assert.match(b.read(b.P.control).final_error, /Fatal configuration/);
  assert.equal(b.spawned.length, 1);
});

test('a STOP left behind while the run is still wanted (window closed) resumes instead of ending it', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.die(); e.writeExit(2, 'stop_file');
  e.advance(SPAWN_GRACE_MS + 1000);
  await e.se.keeper();
  assert.equal(e.spawned.length, 2);
  assert.ok(!fs.existsSync(e.P.stop), 'the stale STOP is cleared before relaunching');
  assert.notEqual(e.read(e.P.control).state, 'stopped');
});

// ---- updates -------------------------------------------------------------------------------------------------
test('before a worker update the run is stopped cleanly, resumed afterwards, and not interrupted again within 30 minutes', async () => {
  const e = makeEnv({ exitOnStop: true });                  // a healthy supervisor leaves as soon as it sees STOP
  await e.se.handleTask(startTask());
  e.advance(SPAWN_GRACE_MS + 1000);
  assert.equal(await e.se.pauseForUpdate(), true);
  assert.equal(e.killed.length, 0, 'a clean exit needs no force');
  assert.equal(e.read(e.P.control).state, 'paused_for_update');
  assert.ok(e.lines(e.P.events).some((x) => x.code === 'paused_for_update'));
  assert.ok(!e.lines(e.P.events).some((x) => x.code === 'update_pause_forced'));
  await e.se.keeper();                                      // inside the pause window: wait for the update to finish
  assert.equal(e.spawned.length, 1);
  e.advance(31_000);
  await e.se.keeper();                                      // the update did not happen (or finished): resume
  assert.equal(e.spawned.length, 2);
  assert.ok(!fs.existsSync(e.P.stop), 'the pause STOP is cleared before relaunching');
  e.advance(SPAWN_GRACE_MS + 1000);
  assert.equal(await e.se.pauseForUpdate(), false, 'a second update attempt 1 minute later is deferred');
  e.advance(UPDATE_DEFER_MS);
  assert.equal(await e.se.pauseForUpdate(), true);
});

test('a supervisor that ignores the pause is ended by force after 3 minutes, and that is audited', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.advance(SPAWN_GRACE_MS + 1000);
  fs.writeFileSync(e.P.runPid, '7777');
  e.alive.add(7777);
  assert.equal(await e.se.pauseForUpdate(), true);
  assert.deepEqual(e.killed.sort(), [5000, 7777]);
  assert.ok(e.lines(e.P.events).some((x) => x.code === 'update_pause_forced' && x.level === 'warn'));
});

test('with nothing running, an update is never held up', async () => {
  assert.equal(await makeEnv().se.pauseForUpdate(), true);
});

// ---- reporting ------------------------------------------------------------------------------------------------
test('events and company results are sent in one report, the cursor advances, and a repeat sends nothing new', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addEvent(1); e.addEvent(2); e.addCompany('IT001'); e.addCompany('IT002');
  fs.writeFileSync(e.P.progress, JSON.stringify({ run_id: RUN, total: 2, done: 2, hits_total: 0, tool_build: 'tb', rules_version: 4, model: 'qwen2.5:3b' }));
  await e.se.reporter(true);
  assert.equal(e.calls.length, 1);
  const rep = e.calls[0].args.p_report;
  assert.equal(e.calls[0].fn, 'vienna_worker_site_evidence_report'); assert.equal(e.calls[0].args.p_token, 'tok');
  assert.equal(rep.run_id, RUN); assert.equal(rep.task_id, 1);
  assert.equal(rep.events.length >= 2, true); assert.deepEqual(rep.companies.map((c) => c.company_id), ['IT001', 'IT002']);
  assert.equal(rep.run.status, 'running'); assert.equal(rep.run.total, 2); assert.equal(rep.run.done, 2);
  assert.equal(rep.run.info.tool_build, 'tb'); assert.equal(rep.run.info.model, 'qwen2.5:3b'); assert.equal(rep.run.info.build, 'wb1');
  const cur = e.cursor();
  assert.ok(cur.events_offset > 0 && cur.outbox_offset > 0);
  await e.se.reporter(true);
  assert.equal(e.calls.length, 1, 'nothing new and the progress report is not due yet');
  e.advance(PROGRESS_REPORT_MS + 1);
  await e.se.reporter(true);
  assert.equal(e.calls.length, 2, 'a progress-only report keeps the database informed');
  assert.equal(e.calls[1].args.p_report.companies.length, 0);
});

test('when the database is unreachable nothing is lost: the cursor stays, it backs off, and it catches up later', async () => {
  const e = makeEnv({ rpcResult: (args, n) => (n <= 2 ? new Error('fetch failed') : { ok: true, error_count: 0, errors: [] }) });
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  await e.se.reporter(true);
  assert.equal(e.cursor().outbox_offset || 0, 0);
  assert.equal(e.read(e.P.reportStatus).ok, false);
  assert.equal(e.se.info().reporting.failures_in_a_row, 1);
  assert.ok(e.se.info().reporting.backlog_outbox_bytes > 0);
  await e.se.reporter();                                    // inside the backoff: no call at all
  assert.equal(e.calls.length, 1);
  e.advance(400_000);
  await e.se.reporter();                                    // second failure
  e.advance(400_000);
  await e.se.reporter();                                    // the database is back
  assert.ok(e.cursor().outbox_offset > 0);
  assert.equal(e.se.info().reporting.failures_in_a_row, 0);
  assert.equal(e.se.info().reporting.backlog_outbox_bytes, 0);
  assert.ok(e.lines(e.P.events).some((x) => x.code === 'report_failed'));
});

test('local launcher runs are left local, and a half-written last line is waited for, never sent torn', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addCompany('IT001', 'local');
  e.addEvent(1, 'local');
  fs.appendFileSync(e.P.outbox, '{"v":1,"run_id":"' + RUN + '","company_id":"IT00');   // torn: no newline yet
  await e.se.reporter(true);
  const sent = e.calls.flatMap((c) => c.args.p_report.companies);
  assert.deepEqual(sent, []);
  assert.ok(e.cursor().outbox_offset > 0, 'the local item was passed over');
  fs.appendFileSync(e.P.outbox, '1","name":"X","rules_version":4,"record":{},"hits":[],"signals":[]}\n');
  await e.se.reporter(true);
  assert.deepEqual(e.calls.flatMap((c) => c.args.p_report.companies).map((c) => c.company_id), ['IT001']);
});

test('a batch the server keeps refusing is halved, narrowed to single records, and the poison record is quarantined', async () => {
  const poison = (args) => args.p_report.companies.some((c) => c.company_id === 'POISON') || args.p_report.events.some((x) => x.message === 'poison event');
  const e = makeEnv({ rpcResult: (args) => (poison(args) ? new Error('vienna_worker_site_evidence_report -> HTTP 500 boom') : { ok: true, error_count: 0, errors: [] }) });
  await e.se.handleTask(startTask());
  for (const r of ['IT001', 'POISON', 'IT002']) e.addCompany(r);
  let guard = 0;
  while (guard++ < 30) {
    e.advance(1);
    await e.se.reporter(true);
    const cur = e.cursor();
    if (cur.outbox_offset === fs.statSync(e.P.outbox).size && cur.events_offset === fs.statSync(e.P.events).size) break;
  }
  const delivered = e.calls.filter((c) => !poison(c.args)).flatMap((c) => c.args.p_report.companies).map((c) => c.company_id);
  assert.deepEqual([...new Set(delivered)].sort(), ['IT001', 'IT002']);
  const q = e.lines(e.P.quarantine);
  assert.equal(q.length, 1); assert.ok(q[0].text.includes('POISON')); assert.match(q[0].reason, /rejected it alone/);
  assert.ok(e.lines(e.P.events).some((x) => x.code === 'report_item_quarantined' && x.level === 'error'));
  assert.equal(e.se.info().reporting.quarantined, 1);
});

test('a company line that cannot be parsed is set aside and does not block the ones behind it', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  fs.appendFileSync(e.P.outbox, 'this is not json\n');
  e.addCompany('IT002');
  await e.se.reporter(true);
  assert.deepEqual(e.calls[0].args.p_report.companies.map((c) => c.company_id), ['IT001', 'IT002']);
  assert.equal(e.lines(e.P.quarantine).length, 1);
});

test('items of two different runs are never mixed in one report', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addCompany('IT001', RUN); e.addCompany('IT002', RUN2);
  await e.se.reporter(true);
  await e.se.reporter(true);
  const byRun = e.calls.map((c) => [c.args.p_report.run_id, c.args.p_report.companies.map((x) => x.company_id)]);
  assert.deepEqual(byRun.filter(([, c]) => c.length), [[RUN, ['IT001']], [RUN2, ['IT002']]]);
});

test('the final report of an ended run is sent once, after which the run is marked as delivered', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  e.die(); e.writeExit(0, 'finished');
  await e.se.keeper();
  await e.se.reporter(true);
  const finals = e.calls.filter((c) => c.args.p_report.run && c.args.p_report.run.status === 'finished');
  assert.equal(finals.length, 1);
  assert.equal(finals[0].args.p_report.run_id, RUN);
  assert.equal(e.read(e.P.control).final_sent, true);
  assert.deepEqual(e.cursor().pending_finals, []);
  await e.se.reporter(true);
  assert.equal(e.calls.filter((c) => c.args.p_report.run && c.args.p_report.run.status === 'finished').length, 1);
});

test('a refused report (revoked worker) is treated as a failure to retry, not as success', async () => {
  const e = makeEnv({ rpcResult: () => ({ ok: false, reason: 'unknown_or_revoked' }) });
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  await e.se.reporter(true);
  assert.equal(e.cursor().outbox_offset || 0, 0);
  assert.match(e.se.info().reporting.last_error, /unknown_or_revoked/);
});

// ---- what the app sees ----------------------------------------------------------------------------------------
test('info() describes the run live: state, progress, what it is waiting for, backlog, and stays small', async () => {
  const e = makeEnv();
  await e.se.capability(true);
  assert.equal(e.se.info().run, null);
  await e.se.handleTask(startTask());
  e.advance(SPAWN_GRACE_MS + 1000);
  fs.writeFileSync(e.P.progress, JSON.stringify({ run_id: RUN, total: 937, done: 12, remaining: 925, hits_total: 1,
    by_status: { done: 11, site_unreachable: 1 }, eta_seconds: 7200, avg_seconds_per_company: 20.5, tool_build: 'tb', rules_version: 4 }));
  fs.writeFileSync(e.P.toolBeat, JSON.stringify({ ts: new Date(e.now).toISOString(), company_id: 'IT005', name: 'ACME', step: 'model:university_partnership', waiting_for: null }));
  let info = e.se.info();
  assert.equal(info.can_start, true); assert.equal(info.run.state, 'running'); assert.equal(info.run.id, RUN);
  assert.equal(info.run.total, 937); assert.equal(info.run.done, 12); assert.equal(info.run.eta_seconds, 7200);
  assert.equal(info.run.current.name, 'ACME'); assert.equal(info.run.pid, 5000);
  fs.writeFileSync(e.P.toolBeat, JSON.stringify({ ts: new Date(e.now).toISOString(), waiting_for: 'ram', name: 'ACME' }));
  assert.equal(e.se.info().run.state, 'waiting_ram');
  fs.writeFileSync(e.P.toolBeat, JSON.stringify({ ts: new Date(e.now - 600_000).toISOString(), waiting_for: 'ram' }));
  assert.equal(e.se.info().run.state, 'running', 'a stale tool heartbeat is not believed');
  e.die();
  assert.equal(e.se.info().run.state, 'starting');
  assert.ok(JSON.stringify(e.se.info()).length < 3500, 'the heartbeat must stay small');
});

test('the diagnostics task returns the tool report plus the worker side, ready to show in the app', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  const d = await e.se.handleTask({ id: 9, kind: 'diagnostics' });
  assert.equal(d.tool.environment.tool_build, 'abc'); assert.equal(d.tool_error, null);
  assert.equal(d.worker.build.build, 'wb1'); assert.equal(d.control.run_id, RUN); assert.equal(d.capability.can_start, true);
});

test('resync makes every local record eligible to be sent again', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  await e.se.reporter(true);
  assert.ok(e.cursor().outbox_offset > 0);
  await e.se.handleTask({ id: 5, kind: 'resync' });
  assert.equal(e.cursor().outbox_offset, 0);
  await e.se.reporter(true);
  assert.equal(e.calls.filter((c) => c.args.p_report.companies.length).length, 2);
});

test('readLinesFrom reads whole lines only, handles multi-byte text, and restarts a truncated file', () => {
  const f = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'se-lines-')), 'x.jsonl');
  fs.writeFileSync(f, '{"a":"università"}\n{"b":2}\n{"c":');
  const r = readLinesFrom(f, 0);
  assert.deepEqual(r.items.map((i) => JSON.parse(i.text)), [{ a: 'università' }, { b: 2 }]);
  assert.equal(r.end, Buffer.byteLength('{"a":"università"}\n{"b":2}\n'));
  assert.equal(readLinesFrom(f, 10_000_000).items.length, 2, 'an offset past the end means the file was replaced: start over');
});

test('a tick with nothing due must not wedge the next ones (found in the end-to-end rehearsal)', async () => {
  const e = makeEnv();
  await e.se.handleTask(startTask());
  await e.se.tick();                       // first tick: capability probe, keeper and reporter all run
  e.addCompany('IT001');
  await e.se.tick();                       // nothing is due yet: completes without awaiting anything
  await e.se.tick();
  e.advance(KEEPER_MS + 30_000);
  e.calls.length = 0;
  await e.se.tick();                       // must still deliver the company that is waiting
  assert.ok(e.calls.some((c) => c.fn === 'vienna_worker_site_evidence_report'), 'the reporter ran after idle ticks');
});

test('tick never throws and does not overlap itself', async () => {
  const e = makeEnv({ rpcResult: () => { throw new Error('boom'); } });
  await e.se.handleTask(startTask());
  e.addCompany('IT001');
  e.advance(KEEPER_MS + 30_000);
  const a = e.se.tick(); const b = e.se.tick();
  assert.equal(a, b);
  await a;
});
