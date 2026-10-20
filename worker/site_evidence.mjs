// Site evidence reader (T26): runs the local-model reader of companies' own websites for the Vienna
// app, for hours or days, with nobody watching.
//
// What the worker is responsible for here, and why it lives in the worker and not in the app:
//   * KEEPING IT ALIVE. A control file (control.json) records that a run is wanted. Whenever the
//     supervisor process is not running and the run is still wanted (a crash, a reboot, an update of
//     this very worker) it is restarted, with backoff and a crash-loop limit. Only the user's Stop, a
//     finished run, or a fatal configuration problem ends it.
//   * REPORTING. The tool keeps an append-only local outbox and audit log. This module forwards them to
//     the database in small idempotent batches through the worker's one allowed door (the shim), keeps
//     a durable cursor, retries with backoff, splits a batch the server refuses, and quarantines a
//     single record that can never be accepted instead of letting it block everything behind it. If the
//     network or the hosting platform is down, nothing is lost: the data waits in the local folder.
//   * SAYING WHAT IT IS DOING. info() is merged into every heartbeat so the app sees the run live;
//     diagnostics() produces the complete picture when something goes wrong.
//
// All outside effects (rpc, spawning, process checks, the clock) are injected, so the logic is tested
// in worker/tests/site_evidence.test.mjs without a network, a database or Python.

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn as nodeSpawn, spawnSync as nodeSpawnSync } from 'node:child_process';

export const KEEPER_MS = 10_000;
export const REPORT_MS = 20_000;
export const PROGRESS_REPORT_MS = 60_000;
export const CAPABILITY_MS = 30_000;
export const RESTART_WINDOW_MS = 15 * 60_000;
export const MAX_RESTARTS_IN_WINDOW = 5;
export const UPDATE_DEFER_MS = 30 * 60_000;
export const RESUME_AFTER_PAUSE_MS = 30_000;
export const SPAWN_GRACE_MS = 45_000;
const RUN_ID = /^[0-9a-f][0-9a-f-]{30,34}[0-9a-f]$/i;
const FINAL_STATES = new Set(['finished', 'stopped', 'failed']);
const DB_STATUS = {
  starting: 'running', running: 'running', waiting_ram: 'running', waiting_model: 'running',
  paused_for_update: 'running', stopping: 'stopping', stopped: 'stopped', finished: 'finished', failed: 'failed',
};

// ---- small file helpers --------------------------------------------------------------------------------
export function readJson(file, fallback = null) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, '')); } catch { return fallback; }
}

export function writeJsonAtomic(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(obj));
  fs.renameSync(tmp, file);
}

function tailText(file, maxBytes = 6000) {
  try {
    const size = fs.statSync(file).size;
    const fd = fs.openSync(file, 'r');
    try {
      const len = Math.min(size, maxBytes);
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, size - len);
      return buf.toString('utf8');
    } finally { fs.closeSync(fd); }
  } catch { return ''; }
}

export function tailLines(file, n = 300, maxBytes = 400_000) {
  const lines = tailText(file, maxBytes).split(/\r?\n/).filter(Boolean);
  return lines.slice(-n);
}

/** Whole lines from `offset`, at most maxBytes/maxItems. `end` is the byte offset just after the last whole line. */
export function readLinesFrom(file, offset, maxBytes = 600_000, maxItems = 1000) {
  let size;
  try { size = fs.statSync(file).size; } catch { return { items: [], end: offset, size: 0 }; }
  if (offset > size) offset = 0;           // the file was replaced or truncated: start over, the database dedupes
  if (offset >= size) return { items: [], end: offset, size };
  const len = Math.min(maxBytes, size - offset);
  const buf = Buffer.alloc(len);
  const fd = fs.openSync(file, 'r');
  try { fs.readSync(fd, buf, 0, len, offset); } finally { fs.closeSync(fd); }
  const lastNl = buf.lastIndexOf(0x0a);
  if (lastNl < 0) {
    // No newline yet: a line still being written, unless it is bigger than the whole window (skip it).
    return len >= maxBytes ? { items: [{ text: null, start: offset, end: offset + len, oversize: true }], end: offset + len, size }
      : { items: [], end: offset, size };
  }
  const items = [];
  let pos = 0;
  const whole = buf.subarray(0, lastNl + 1);
  while (pos < whole.length && items.length < maxItems) {
    const nl = whole.indexOf(0x0a, pos);
    const text = whole.subarray(pos, nl).toString('utf8').replace(/\r$/, '');
    items.push({ text, start: offset + pos, end: offset + nl + 1 });
    pos = nl + 1;
  }
  return { items, end: items.length ? items[items.length - 1].end : offset, size };
}

function appendLine(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, JSON.stringify(obj) + '\n');
}

function csvRowCount(text) {
  const lines = String(text).split(/\r?\n/).filter((l) => l.trim());
  return Math.max(0, lines.length - 1);
}

// ---- defaults for the injected effects ----------------------------------------------------------------
function defaultPidAlive(pid) {
  if (!pid || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

function defaultKillTree(pid) {
  if (!pid || pid <= 0) return;
  try {
    if (process.platform === 'win32') nodeSpawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], { windowsHide: true });
    else process.kill(-pid, 'SIGKILL');
  } catch { /* already gone */ }
}

function ollamaExeFound() {
  const local = process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Programs', 'Ollama', 'ollama.exe') : '';
  if (local && fs.existsSync(local)) return true;
  try {
    const r = nodeSpawnSync(process.platform === 'win32' ? 'where' : 'which', ['ollama'], { windowsHide: true, timeout: 4000 });
    return r.status === 0;
  } catch { return false; }
}

async function defaultProbeOllama() {
  let reachable = false;
  let models = [];
  try {
    const res = await fetch('http://127.0.0.1:11434/api/tags', { signal: AbortSignal.timeout(2500) });
    if (res.ok) {
      reachable = true;
      models = ((await res.json()).models || []).map((m) => String(m.name));
    }
  } catch { /* not running */ }
  return { installed: reachable || ollamaExeFound(), reachable, models };
}

const WANTED_MODELS = ['qwen2.5:3b', 'qwen2.5-3b-8k'];
const modelOk = (models) => models.some((m) => WANTED_MODELS.some((w) => m === w || m.startsWith(`${w}:`)));

// ---- the module -----------------------------------------------------------------------------------------
export function createSiteEvidence(deps) {
  const {
    root, token, rpc, log = () => {},
    outDir = path.join(os.homedir(), 'SiteEvidenceOutput'),
    toolDir = path.join(root, 'tools', 'site_evidence'),
    now = () => Date.now(),
    sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
    pythonCommand = 'python',
    spawnProcess = nodeSpawn,
    spawnSyncProcess = nodeSpawnSync,
    pidAlive = defaultPidAlive,
    killTree = defaultKillTree,
    probeOllama = defaultProbeOllama,
    workerInfo = () => ({}),
    workerDiagnostics = () => ({}),
    freeGb = { toLoad: 1.0, loaded: 0.3 },
  } = deps;

  const P = {
    control: path.join(outDir, 'control.json'), progress: path.join(outDir, 'progress.json'),
    toolBeat: path.join(outDir, 'heartbeat.json'), outbox: path.join(outDir, 'outbox.jsonl'),
    events: path.join(outDir, 'events.jsonl'), cursor: path.join(outDir, 'report_cursor.json'),
    reportStatus: path.join(outDir, 'report_status.json'), quarantine: path.join(outDir, 'quarantine.jsonl'),
    exit: path.join(outDir, 'supervisor_exit.json'), supPid: path.join(outDir, 'supervisor.pid'),
    runPid: path.join(outDir, 'runner.pid'), stop: path.join(outDir, 'STOP'), companies: path.join(outDir, 'companies.csv'),
    spawnLog: path.join(outDir, 'supervisor_spawn.log'),
  };

  let capCache = null;
  let capAt = 0;
  let lastKeeper = 0;
  let lastReportAttempt = 0;
  let lastProgressReport = 0;
  let reportFailures = 0;
  let backoffUntil = 0;
  let nextRestartAt = 0;
  let lastPauseAt = 0;
  let busy = null;
  let ticking = false;
  let degraded = false;     // the server refused even a minimal batch: send one record per call to isolate the bad one
  let limits = { events: 150, companies: 40 };
  let lastReportOk = null;
  let lastReportError = null;

  // ---- audit events written by the worker itself --------------------------------------------------------
  function event(level, code, message, data = {}, runId = null) {
    const c = readJson(P.control);
    const ev = {
      id: crypto.randomBytes(16).toString('hex'), ts: new Date(now()).toISOString(), source: 'worker',
      run_id: runId || (c && c.run_id) || 'local', level, code, message: String(message).slice(0, 600), data,
    };
    try { appendLine(P.events, ev); } catch (e) { log(`could not write an audit event: ${e.message}`); }
    return ev;
  }

  const control = () => readJson(P.control);
  function saveControl(patch) {
    const next = { ...(control() || {}), ...patch };
    writeJsonAtomic(P.control, next);
    return next;
  }
  function safeRead(file) { try { return fs.readFileSync(file, 'utf8'); } catch { return ''; } }
  const supervisorPid = () => Number(safeRead(P.supPid).trim()) || 0;
  const supervisorAlive = () => pidAlive(supervisorPid());

  // ---- capability: can this computer run it at all? ------------------------------------------------------
  async function capability(force = false) {
    if (!force && capCache && now() - capAt < CAPABILITY_MS) return capCache;
    const available = fs.existsSync(path.join(toolDir, 'runner.py'));
    let python = { ok: false, detail: 'not checked' };
    if (available) {
      const r = spawnSyncProcess(pythonCommand, ['--version'], { encoding: 'utf8', timeout: 5000, windowsHide: true });
      const out = `${r.stdout || ''}${r.stderr || ''}`.trim();
      python = r.status === 0 ? { ok: true, detail: out } : { ok: false, detail: 'Python 3 is not available on this computer' };
    }
    let ollama = { installed: false, reachable: false, models: [] };
    try { ollama = await probeOllama(); } catch { /* keep the pessimistic default */ }
    const blockers = [];
    if (!available) blockers.push('This worker build does not include the site evidence reader yet (it updates itself within minutes of a new release)');
    else {
      if (!python.ok) blockers.push(python.detail);
      if (!ollama.installed) blockers.push('Ollama (the local model) is not installed on this computer');
      else if (ollama.reachable && !modelOk(ollama.models)) {
        blockers.push(`The model qwen2.5:3b is not installed on this computer (found: ${ollama.models.join(', ') || 'none'})`);
      }
    }
    capCache = { available, python, ollama: { ...ollama, model_ok: ollama.reachable ? modelOk(ollama.models) : null },
      can_start: blockers.length === 0, blockers };
    capAt = now();
    return capCache;
  }

  // ---- what the heartbeat says ------------------------------------------------------------------------
  function lastEvent() {
    const lines = tailText(P.events, 8000).split('\n').filter(Boolean);
    for (let i = lines.length - 1; i >= 0; i--) {
      try { const e = JSON.parse(lines[i]); return { ts: e.ts, level: e.level, code: e.code, message: String(e.message).slice(0, 160) }; } catch { /* torn line */ }
    }
    return null;
  }

  function currentState(c) {
    if (!c || !c.run_id) return null;
    if (FINAL_STATES.has(c.state)) return c.state;
    if (c.state === 'paused_for_update' || c.state === 'stopping') return c.state;
    if (!supervisorAlive()) return c.desired === 'running' ? 'starting' : c.state || 'stopped';
    const beat = readJson(P.toolBeat, {}) || {};
    const fresh = beat.ts && now() - Date.parse(beat.ts) < 180_000;
    if (fresh && beat.waiting_for === 'ram') return 'waiting_ram';
    if (fresh && beat.waiting_for === 'model') return 'waiting_model';
    return 'running';
  }

  function backlog() {
    const cur = readJson(P.cursor, {}) || {};
    const size = (f) => { try { return fs.statSync(f).size; } catch { return 0; } };
    return { outbox_bytes: Math.max(0, size(P.outbox) - (cur.outbox_offset || 0)),
      events_bytes: Math.max(0, size(P.events) - (cur.events_offset || 0)) };
  }

  function info() {
    const cap = capCache || { available: fs.existsSync(path.join(toolDir, 'runner.py')), python: null, ollama: null,
      can_start: false, blockers: ['checking this computer...'] };
    const c = control();
    const progress = readJson(P.progress, null);
    const beat = readJson(P.toolBeat, null);
    const state = currentState(c);
    let run = null;
    if (c && c.run_id) {
      const sameRun = progress && progress.run_id === c.run_id;
      run = {
        id: c.run_id, desired: c.desired, state, since: c.started_at, finished_at: c.finished_at || null,
        pid: supervisorPid() || null, restarts: (c.restarts || []).length,
        total: sameRun ? progress.total : (c.total ?? null), done: sameRun ? progress.done : null,
        remaining: sameRun ? progress.remaining : null, hits_total: sameRun ? progress.hits_total : null,
        by_status: sameRun ? progress.by_status : null, eta_seconds: sameRun ? progress.eta_seconds : null,
        avg_seconds: sameRun ? progress.avg_seconds_per_company : null,
        current: beat ? { company_id: beat.company_id, name: beat.name, step: beat.step } : null,
        waiting_for: beat && beat.waiting_for ? beat.waiting_for : null,
        last_event: lastEvent(), error: c.final_error || null,
      };
    }
    const b = backlog();
    return {
      available: cap.available, python: cap.python, ollama: cap.ollama, can_start: cap.can_start, blockers: cap.blockers,
      run, tool_build: (progress && progress.tool_build) || null, rules_version: (progress && progress.rules_version) || null,
      reporting: { backlog_outbox_bytes: b.outbox_bytes, backlog_events_bytes: b.events_bytes, last_ok_at: lastReportOk,
        last_error: lastReportError, failures_in_a_row: reportFailures,
        quarantined: (() => { try { return fs.readFileSync(P.quarantine, 'utf8').split('\n').filter(Boolean).length; } catch { return 0; } })() },
    };
  }

  // ---- tasks from the app --------------------------------------------------------------------------------
  async function handleTask(task) {
    const req = task.request || {};
    if (task.kind === 'start') return start(task, req);
    if (task.kind === 'stop') return stop(task);
    if (task.kind === 'diagnostics') return diagnostics();
    if (task.kind === 'resync') return resync();
    throw new Error(`unknown site_evidence task '${task.kind}'`);
  }

  async function start(task, req) {
    const runId = String(req.run_id || '');
    if (!RUN_ID.test(runId)) throw new Error('the start request has no valid run id');
    // A refused start must not leave the run "queued" in the app forever: its final report says why.
    const refuse = (message, data = {}) => {
      event('error', 'start_refused', `The run could not be started: ${message}`, data, runId);
      queueFinal({ run_id: runId, state: 'failed', finished_at: new Date(now()).toISOString(), final_error: `Not started: ${message}` });
      return new Error(message);
    };
    const inputCsv = String(req.input_csv || '');
    if (csvRowCount(inputCsv) < 1 || !/company_id/i.test(inputCsv.split('\n')[0])) throw refuse('the start request has no company list');
    const cap = await capability(true);
    if (!cap.can_start) throw refuse(cap.blockers.join('; '), { blockers: cap.blockers });
    const existing = control();
    if (existing && existing.run_id && existing.desired === 'running' && !FINAL_STATES.has(existing.state)) {
      if (existing.run_id === runId) return { run_id: runId, already_running: true };
      throw new Error(`a run is already active on this computer (${existing.run_id}); stop it first`);
    }
    // A run that ended without its final report ever reaching the database gets it sent first.
    if (existing && existing.run_id && FINAL_STATES.has(existing.state) && !existing.final_sent) queueFinal(existing);

    fs.mkdirSync(outDir, { recursive: true });
    const tmp = `${P.companies}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, inputCsv, 'utf8');
    fs.renameSync(tmp, P.companies);
    for (const f of [P.stop, P.exit]) { try { fs.rmSync(f, { force: true }); } catch { /* ignore */ } }
    const options = req.options || {};
    const total = csvRowCount(inputCsv);
    writeJsonAtomic(P.control, {
      run_id: runId, desired: 'running', state: 'starting', task_id: task.id, started_at: new Date(now()).toISOString(),
      options, total, restarts: [], final_sent: false,
    });
    event('info', 'run_requested', `Run ${runId} requested from the app: ${total} companies`, {
      options, total, task_id: task.id, worker: workerInfo(), blockers: [] }, runId);
    spawnSupervisor(control(), 'start');
    lastProgressReport = 0; // tell the database the run is under way at the next report
    return { run_id: runId, started: true, total };
  }

  async function stop(task) {
    const c = control();
    if (!c || !c.run_id || FINAL_STATES.has(c.state)) return { stopped: false, reason: 'no active run' };
    saveControl({ desired: 'stopped', state: 'stopping', stop_requested_at: new Date(now()).toISOString() });
    try { fs.writeFileSync(P.stop, `stop requested from the app (task ${task.id})`); } catch { /* the keeper retries */ }
    event('info', 'stop_requested', 'A stop was requested from the app; the run finishes its current step, saves, and exits', { task_id: task.id }, c.run_id);
    lastProgressReport = 0;
    return { stop_requested: true, run_id: c.run_id };
  }

  function resync() {
    writeJsonAtomic(P.cursor, { ...(readJson(P.cursor, {}) || {}), outbox_offset: 0, events_offset: 0 });
    event('info', 'resync_requested', 'Every local result and event will be sent to the database again (it ignores what it already has)');
    lastProgressReport = 0;
    return { resync: true };
  }

  async function diagnostics() {
    const r = spawnSyncProcess(pythonCommand, ['-u', '-m', 'tools.site_evidence.diagnostics', '--output-dir', outDir, '--compact'], {
      cwd: root, encoding: 'utf8', timeout: 90_000, windowsHide: true, maxBuffer: 40 * 1024 * 1024,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    });
    let tool = null;
    let toolError = null;
    try { tool = JSON.parse(r.stdout); } catch { toolError = `the diagnostics collector failed (exit ${r.status}): ${String(r.stderr || r.error || '').slice(-1500)}`; }
    const out = { collected_at: new Date(now()).toISOString(), worker: { ...workerDiagnostics(), build: workerInfo() },
      tool, tool_error: toolError, capability: await capability(true), control: control(), info: info(),
      spawn_log_tail: tailLines(P.spawnLog, 80), report_status: readJson(P.reportStatus) };
    if (JSON.stringify(out).length > 3_000_000 && tool) { tool.log_tails = '<dropped: result too large>'; }
    return out;
  }

  // ---- keeping the run alive ------------------------------------------------------------------------------
  function spawnSupervisor(c, why) {
    try { fs.rmSync(P.stop, { force: true }); } catch { /* ignore */ }
    try { fs.rmSync(P.exit, { force: true }); } catch { /* ignore */ }
    const opts = c.options || {};
    const args = ['-u', '-m', 'tools.site_evidence.supervisor', '--input', P.companies, '--output-dir', outDir,
      '--run-id', c.run_id, '--min-free-gb-to-load', String(opts.min_free_gb_to_load ?? freeGb.toLoad),
      '--min-free-gb-loaded', String(opts.min_free_gb_loaded ?? freeGb.loaded),
      '--retry-after-hours', String(opts.retry_after_hours ?? 6)];
    let fd = null;
    try {
      fs.mkdirSync(outDir, { recursive: true });
      fd = fs.openSync(P.spawnLog, 'a');
      const child = spawnProcess(pythonCommand, args, {
        cwd: root, detached: true, windowsHide: true, stdio: ['ignore', fd, fd],
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      });
      if (child && child.unref) child.unref();
      saveControl({ state: 'starting', last_spawn_at: new Date(now()).toISOString() });
      event('info', 'supervisor_launched', `Supervisor launched (${why})`, { pid: child && child.pid, args: args.slice(5) }, c.run_id);
      return true;
    } catch (e) {
      event('error', 'supervisor_launch_failed', `Could not launch the supervisor: ${e.message}`, { error: String(e.message) }, c.run_id);
      return false;
    } finally { if (fd !== null) { try { fs.closeSync(fd); } catch { /* ignore */ } } }
  }

  function queueFinal(c) {
    const cur = readJson(P.cursor, {}) || {};
    const finals = (cur.pending_finals || []).filter((f) => f.run_id !== c.run_id);
    finals.push({ run_id: c.run_id, status: c.state, finished_at: c.finished_at || new Date(now()).toISOString(), error: c.final_error || null });
    writeJsonAtomic(P.cursor, { ...cur, pending_finals: finals });
  }

  function finalize(c, status, error = null, extra = {}) {
    const finishedAt = new Date(now()).toISOString();
    const next = saveControl({ desired: 'stopped', state: status, finished_at: finishedAt, final_error: error, final_sent: false });
    event(status === 'failed' ? 'error' : 'info', `run_${status}`,
      status === 'finished' ? 'The run finished: every company that was due has been read'
        : status === 'stopped' ? 'The run was stopped' : `The run FAILED: ${error}`, { error, ...extra }, c.run_id);
    queueFinal(next);
    lastProgressReport = 0;
    if (status === 'failed') {
      // Everything needed to fix it, attached to the audit trail while the evidence is still fresh.
      try {
        const r = spawnSyncProcess(pythonCommand, ['-u', '-m', 'tools.site_evidence.diagnostics', '--output-dir', outDir, '--compact'], {
          cwd: root, encoding: 'utf8', timeout: 60_000, windowsHide: true, maxBuffer: 40 * 1024 * 1024,
          env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } });
        const d = JSON.parse(r.stdout);
        event('error', 'failure_diagnostics', 'Diagnostics captured at the moment the run failed', {
          problem_companies: (d.problem_companies || []).slice(0, 20), recent: (d.events && d.events.recent_warnings_and_errors || []).slice(-15),
          resources: d.resources, model_server: d.model_server, processes: d.processes, state: d.state,
          log_tail: ((d.log_tails || {})['run.log'] || []).slice(-40), supervisor_tail: ((d.log_tails || {})['supervisor.log'] || []).slice(-20),
          spawn_log_tail: tailLines(P.spawnLog, 40) }, c.run_id);
      } catch (e) {
        event('error', 'failure_diagnostics', `Could not capture diagnostics: ${e.message}`, { spawn_log_tail: tailLines(P.spawnLog, 40) }, c.run_id);
      }
    }
  }

  async function keeper() {
    const c = control();
    if (!c || !c.run_id || FINAL_STATES.has(c.state)) return;
    const alive = supervisorAlive();

    if (c.desired === 'stopped') {
      if (alive) { try { fs.writeFileSync(P.stop, 'stop requested'); } catch { /* retry next tick */ } return; }
      finalize(c, 'stopped');
      return;
    }
    if (alive) {
      const st = currentState(c);
      if (st !== c.state && st) saveControl({ state: st });
      return;
    }
    const pausedAt = c.paused_at ? Date.parse(c.paused_at) : 0;
    if (c.state === 'paused_for_update' && pausedAt && now() - pausedAt < RESUME_AFTER_PAUSE_MS) return;
    // A supervisor launched moments ago needs time to start (Python imports, the pid file): do not mistake
    // that for a crash and launch a second one.
    const spawnedAt = c.last_spawn_at ? Date.parse(c.last_spawn_at) : 0;
    if (spawnedAt && now() - spawnedAt < SPAWN_GRACE_MS && !readJson(P.exit, null)) return;

    const exit = readJson(P.exit, null);
    const exitHere = exit && exit.run_id === c.run_id ? exit : null;
    if (exitHere && exitHere.code === 0 && exitHere.reason === 'finished') { finalize(c, 'finished'); return; }
    if (exitHere && exitHere.code === 3) {
      finalize(c, 'failed', `Fatal configuration problem (${exitHere.reason}); the run cannot continue until this is fixed. See the failure diagnostics event`);
      return;
    }
    if (now() < nextRestartAt) return;

    const recent = (c.restarts || []).filter((t) => now() - t < RESTART_WINDOW_MS);
    if (recent.length >= MAX_RESTARTS_IN_WINDOW) {
      finalize(c, 'failed', `Crash loop: the run was restarted ${recent.length} times in ${RESTART_WINDOW_MS / 60000} minutes and kept dying`);
      return;
    }
    const why = exitHere ? `the supervisor ended (code ${exitHere.code}, ${exitHere.reason})` : 'the supervisor is not running (crash, reboot or update)';
    event('warn', 'supervisor_restarted', `Restarting the run: ${why}`, { exit: exitHere, restarts_in_window: recent.length,
      spawn_log_tail: tailLines(P.spawnLog, 25) }, c.run_id);
    saveControl({ restarts: [...recent, now()] });
    nextRestartAt = now() + Math.min(300_000, 15_000 * 2 ** recent.length);
    spawnSupervisor(control(), c.state === 'paused_for_update' ? 'resumed after an update pause' : 'restarted');
  }

  /** Stops the run cleanly before this worker replaces its own installation; keeper resumes it afterwards. */
  async function pauseForUpdate() {
    const c = control();
    if (!c || !c.run_id || c.desired !== 'running' || FINAL_STATES.has(c.state) || !supervisorAlive()) return true;
    if (now() - lastPauseAt < UPDATE_DEFER_MS) return false;       // do not interrupt a long run every five minutes
    lastPauseAt = now();
    saveControl({ state: 'paused_for_update', paused_at: new Date(now()).toISOString() });
    event('info', 'paused_for_update', 'Pausing the run so the worker can update itself; it resumes automatically afterwards', {}, c.run_id);
    try { fs.writeFileSync(P.stop, 'paused for a worker update'); } catch { /* the kill below covers it */ }
    for (let i = 0; i < 90 && supervisorAlive(); i++) await sleep(2000);
    if (supervisorAlive()) {
      killTree(supervisorPid());
      const runnerPid = Number(safeRead(P.runPid).trim()) || 0;
      if (runnerPid) killTree(runnerPid);
      event('warn', 'update_pause_forced', 'The run did not stop in 3 minutes; it was ended by force (progress is saved per company)', {}, c.run_id);
    }
    return true;
  }

  // ---- reporting to the database ---------------------------------------------------------------------------
  /** Parsed lines in file order. A line that cannot be parsed is kept as `bad` so it is consumed (and set aside) in order. */
  function parseItems(items) {
    return items.map((it) => {
      if (it.oversize) return { obj: null, start: it.start, end: it.end, raw: null, bad: 'a single line larger than the read window' };
      try { return { obj: JSON.parse(it.text), start: it.start, end: it.end, raw: it.text, bad: null }; }
      catch (e) { return { obj: null, start: it.start, end: it.end, raw: it.text, bad: `unparseable line: ${e.message}` }; }
    });
  }

  const quarantinedOnce = new Set();
  function quarantine(file, text, reason, at) {
    const key = `${path.basename(file)}:${at}`;
    if (quarantinedOnce.has(key)) return;
    quarantinedOnce.add(key);
    try { appendLine(P.quarantine, { at: new Date(now()).toISOString(), file: path.basename(file), offset: at, reason, text }); } catch { /* ignore */ }
    event('error', 'report_item_quarantined', `A local record could not be sent and was set aside: ${reason}`, { file: path.basename(file), offset: at });
  }

  function runObject(c, state) {
    const progress = readJson(P.progress, null);
    const sameRun = progress && progress.run_id === c.run_id;
    // A run that has ended is reported as ended only by its final report, which goes out after the results;
    // until then the database keeps saying "running" (results still being delivered), never a premature "finished".
    return {
      status: FINAL_STATES.has(state) ? 'running' : (DB_STATUS[state] || 'running'), started_at: c.started_at || null,
      total: sameRun ? progress.total : (c.total ?? null), done: sameRun ? progress.done : null,
      hits_total: sameRun ? progress.hits_total : null,
      summary: sameRun ? progress : undefined,
      info: { ...workerInfo(), tool_build: progress && progress.tool_build, rules_version: progress && progress.rules_version,
        model: progress && progress.model, options: c.options || {}, restarts: (c.restarts || []).length },
    };
  }

  const isUuidRun = (id) => RUN_ID.test(String(id || ''));

  /**
   * The next batch, taken strictly in file order. Items of local launcher runs ("local") are not the app's
   * runs: they are passed over (they stay in the local folder and are imported by script). A line that cannot
   * be parsed is set aside. Only items of ONE run go in a batch, so every report is attributed correctly.
   * In `single` mode (a batch the server refused, already at its smallest) the batch is exactly one record,
   * so the cause can be isolated.
   */
  function buildBatch(cur, single = false) {
    const c = control();
    const evLimit = single ? 1 : limits.events;
    const obLimit = single ? 1 : limits.companies;
    const ev = readLinesFrom(P.events, cur.events_offset || 0, 500_000, Math.max(evLimit, 1));
    const ob = readLinesFrom(P.outbox, cur.outbox_offset || 0, 900_000, Math.max(obLimit, 1));
    const evItems = parseItems(ev.items);
    const obItems = parseItems(ob.items);
    const firstReal = (list) => list.find((x) => !x.bad && isUuidRun(x.obj.run_id));
    const first = firstReal(obItems) || firstReal(evItems);
    const target = first ? first.obj.run_id : (c && c.run_id) || null;
    const takeWhile = (list, file) => {
      const taken = [];
      for (const x of list) {
        if (x.bad) { quarantine(file, x.raw && x.raw.slice(0, 500), x.bad, x.start); taken.push({ ...x, skip: true }); continue; }
        if (!isUuidRun(x.obj.run_id)) { taken.push({ ...x, skip: true }); continue; }
        if (x.obj.run_id !== target) break;
        taken.push(x);
      }
      return taken;
    };
    let tCompanies = takeWhile(obItems, P.outbox);
    let tEvents = takeWhile(evItems, P.events);
    if (single && tCompanies.some((x) => !x.skip)) tEvents = []; // one record at a time: companies first
    const end = (taken, fallback) => (taken.length ? taken[taken.length - 1].end : fallback);
    return {
      target, c,
      events: tEvents.filter((x) => !x.skip).map((x) => x.obj),
      companies: tCompanies.filter((x) => !x.skip).map((x) => x.obj),
      eventsEnd: end(tEvents, cur.events_offset || 0), companiesEnd: end(tCompanies, cur.outbox_offset || 0),
      eventItems: tEvents, companyItems: tCompanies,
    };
  }

  const isDataError = (e) => /HTTP (400|413|422|500)\b/.test(String(e && e.message));

  async function sendReport(report) {
    const res = await rpc('vienna_worker_site_evidence_report', { p_token: token, p_report: report });
    if (!res || res.ok === false) {
      const reason = res && res.reason ? res.reason : 'no answer';
      const err = new Error(`the database refused the report: ${reason}`);
      err.refused = reason;
      throw err;
    }
    return res;
  }

  function recordStatus(patch) {
    try { writeJsonAtomic(P.reportStatus, { ...(readJson(P.reportStatus, {}) || {}), ...patch, at: new Date(now()).toISOString() }); } catch { /* ignore */ }
  }

  /** Delivers everything that is waiting, a batch at a time, then the final reports owed for runs that ended. */
  async function reporter(force = false) {
    if (!force && now() < backoffUntil) return;
    lastReportAttempt = now();
    for (let round = 0; round < 6; round++) {
      if (!(await reportRound())) break;
    }
  }

  /** The final status of an ended run goes out only AFTER its results, so the app never says "finished" while
   *  results are still on the way. */
  async function sendPendingFinal() {
    const f = ((readJson(P.cursor, {}) || {}).pending_finals || [])[0];
    if (!f) return false;
    try {
      await sendReport({ run_id: f.run_id, run: { status: f.status, finished_at: f.finished_at, last_error: f.error }, events: [], companies: [] });
      const left = readJson(P.cursor, {}) || {};
      writeJsonAtomic(P.cursor, { ...left, pending_finals: (left.pending_finals || []).filter((x) => x.run_id !== f.run_id) });
      const c = control();
      if (c && c.run_id === f.run_id) saveControl({ final_sent: true });
      reportFailures = 0; lastReportError = null; lastReportOk = new Date(now()).toISOString();
      recordStatus({ ok: true, last_ok_at: lastReportOk, last_error: null, final_sent_for: f.run_id });
      return true;
    } catch (e) { onReportFailure(e); return false; }
  }

  /** One unit of reporting. Returns true when something was done and there may be more to do. */
  async function reportRound() {
    const cur = readJson(P.cursor, {}) || {};
    const c = control();
    const batch = buildBatch(cur, degraded);
    const empty = !batch.events.length && !batch.companies.length;
    const consumedSomething = batch.eventsEnd !== (cur.events_offset || 0) || batch.companiesEnd !== (cur.outbox_offset || 0);
    const state = c ? currentState(c) : null;
    const dueProgress = c && c.run_id && !FINAL_STATES.has(state) && now() - lastProgressReport >= PROGRESS_REPORT_MS;
    if (empty && !consumedSomething && !dueProgress) return sendPendingFinal();
    const target = batch.target || (c && c.run_id);
    if (!target || !isUuidRun(target)) {
      if (consumedSomething) writeJsonAtomic(P.cursor, { ...(readJson(P.cursor, {}) || {}), events_offset: batch.eventsEnd, outbox_offset: batch.companiesEnd });
      return false;
    }
    const report = { run_id: target, task_id: c && c.run_id === target ? (c.task_id ?? null) : null,
      run: c && c.run_id === target ? runObject(c, state) : {}, events: batch.events, companies: batch.companies };
    lastReportAttempt = now();
    try {
      const res = await sendReport(report);
      const fresh = readJson(P.cursor, {}) || {};
      writeJsonAtomic(P.cursor, { ...fresh, events_offset: batch.eventsEnd, outbox_offset: batch.companiesEnd, last_ok_at: new Date(now()).toISOString() });
      lastProgressReport = now();
      reportFailures = 0; lastReportError = null; lastReportOk = new Date(now()).toISOString();
      limits = { events: Math.min(150, limits.events * 2), companies: Math.min(40, limits.companies * 2) };
      degraded = false;
      if (res.error_count > 0) {
        event('warn', 'report_partial_errors', `The database accepted the batch but declined ${res.error_count} item(s)`, { errors: res.errors }, target);
      }
      recordStatus({ ok: true, last_ok_at: lastReportOk, last_error: null, sent: { events: batch.events.length, companies: batch.companies.length }, declined: res.error_count });
      return !empty || consumedSomething;   // there may be more waiting behind this batch
    } catch (e) {
      if (isDataError(e) && (batch.events.length + batch.companies.length) > 0) {
        if (batch.events.length + batch.companies.length > 1) {
          const atMinimum = limits.events <= 1 && limits.companies <= 1;
          if (atMinimum) degraded = true;               // one record per call from now on
          else limits = { events: Math.max(1, Math.floor(limits.events / 2)), companies: Math.max(1, Math.floor(limits.companies / 2)) };
          recordStatus({ ok: false, last_error: String(e.message).slice(0, 400), splitting: limits, single_record_mode: degraded });
          event('warn', 'report_batch_split', 'The server refused a batch; retrying in smaller pieces to isolate the cause', { error: String(e.message).slice(0, 300), limits, single_record_mode: degraded }, target);
          return true;
        }
        // One single item that the server can never accept: set it aside so everything behind it can flow.
        const item = batch.companyItems.find((x) => !x.skip) || batch.eventItems.find((x) => !x.skip);
        const fromOutbox = batch.companyItems.includes(item);
        quarantine(fromOutbox ? P.outbox : P.events, item.raw.slice(0, 4000), `the server rejected it alone: ${String(e.message).slice(0, 300)}`, item.start);
        const fresh = readJson(P.cursor, {}) || {};
        writeJsonAtomic(P.cursor, { ...fresh, ...(fromOutbox ? { outbox_offset: item.end } : { events_offset: item.end }) });
        return true;
      }
      onReportFailure(e);
      return false;
    }
  }

  function onReportFailure(e) {
    reportFailures += 1;
    lastReportError = String(e.message).slice(0, 300);
    backoffUntil = now() + Math.min(300_000, 15_000 * 2 ** Math.min(reportFailures, 5));
    recordStatus({ ok: false, last_error: lastReportError, failures_in_a_row: reportFailures, retry_at: new Date(backoffUntil).toISOString() });
    if (reportFailures === 1 || reportFailures % 20 === 0) {
      event('warn', 'report_failed', `Could not reach the database (${lastReportError}); results stay on this computer and are sent when it answers`, { failures_in_a_row: reportFailures });
    }
  }

  // ---- the loop hook ---------------------------------------------------------------------------------------
  /** Call from the worker's main loop; never throws and never blocks it. */
  function tick() {
    // A flag, not the promise itself, is the gate: a tick with nothing due finishes without ever awaiting, so its
    // `finally` runs BEFORE the promise is assigned and a promise-based gate would stay set forever.
    if (ticking) return busy;
    ticking = true;
    busy = (async () => {
      try {
        if (now() - capAt >= CAPABILITY_MS) await capability(true);
        if (now() - lastKeeper >= KEEPER_MS) { lastKeeper = now(); await keeper(); }
        if (now() - lastReportAttempt >= REPORT_MS) await reporter();
      } catch (e) { log(`site evidence tick failed: ${String(e.message).slice(0, 300)}`); }
      finally { ticking = false; }
    })();
    return busy;
  }

  return { info, handleTask, tick, pauseForUpdate, capability, diagnostics, keeper, reporter, control, paths: P };
}
