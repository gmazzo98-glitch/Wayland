// Vienna Crawler Worker — runs the Vienna crawlers on this computer when the Vienna app asks.
//
// It holds no database password. It only knows the project's public API key and its own token,
// and talks to three token-checked functions (worker/supabase_rpc.sql on the server):
//   heartbeat  every ~15s: "I'm here" + version + self-test results (this is how the app
//              knows the install is present, current and healthy)
//   claim      every ~2s while it has a free slot: "give me my next queued crawl"
//   complete   posts the crawl's rows (or its error) back
//
// Everything is Node built-ins — nothing to install for the worker itself.
//
//   node worker.mjs              run the worker (what the startup shortcut does)
//   node worker.mjs --selftest   check the installation and print a report (used by the installer)

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = process.env.VIENNA_WORKER_HOME ? path.resolve(process.env.VIENNA_WORKER_HOME) : path.resolve(HERE, '..');
const CRAWLERS_DIR = path.join(ROOT, 'crawlers');
const BROWSERS_DIR = path.join(ROOT, 'browsers');
const LOG_FILE = path.join(ROOT, 'worker.log');
const STATUS_FILE = path.join(ROOT, 'status.json');
const LOCK_PORT_FILE = path.join(ROOT, 'worker.pid');

const HEARTBEAT_MS = 15_000;
const CLAIM_MS = 2_000;
const SELFTEST_EVERY_MS = 30 * 60_000;
const RPC_TIMEOUT_MS = 25_000;
const PROTOCOL = 1;

// Crawler subprocesses may only receive these settings from the app. The task queue is
// trusted, but a worker should still never let it inject things like NODE_OPTIONS or PATH.
const ENV_ALLOWLIST = new Set([
  'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL', 'WAYBACK_DELAY_MS', 'SEARCH_PROVIDER',
  'NEWSAPI_KEY', 'ANTHROPIC_API_KEY', 'BUILTWITH_API_KEY', 'LINKEDIN_LI_AT',
]);

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
}

let CONFIG;
let VERSION_INFO;
try {
  CONFIG = readJson(path.join(ROOT, 'worker.config.json'));
  VERSION_INFO = readJson(path.join(ROOT, 'VERSION.json'));
} catch (e) {
  console.error(`Vienna Crawler Worker is not installed correctly (${e.message}). Run the setup file again.`);
  process.exit(2);
}

// ---- logging ---------------------------------------------------------------------------

function log(msg) {
  const line = `${new Date().toISOString()}  ${msg}`;
  try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch { /* logging must never crash the worker */ }
  if (process.stdout.isTTY) console.log(line);
}

function trimLog() {
  try {
    if (fs.statSync(LOG_FILE).size > 2_000_000) fs.renameSync(LOG_FILE, LOG_FILE + '.old');
  } catch { /* no log yet */ }
}

function writeStatus(patch) {
  let cur = {};
  try { cur = readJson(STATUS_FILE); } catch { /* first write */ }
  try { fs.writeFileSync(STATUS_FILE, JSON.stringify({ ...cur, ...patch }, null, 2)); } catch { /* best effort */ }
}

// ---- talking to the server -----------------------------------------------------------------

const KEY = CONFIG.anonKey;
async function rpc(fn, args) {
  const headers = { apikey: KEY, 'Content-Type': 'application/json' };
  if (KEY.startsWith('eyJ')) headers.Authorization = `Bearer ${KEY}`; // legacy JWT keys want it; sb_publishable_ keys don't
  const res = await fetch(`${CONFIG.supabaseUrl.replace(/\/$/, '')}/rest/v1/rpc/${fn}`, {
    method: 'POST', headers, body: JSON.stringify(args), signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`${fn} -> HTTP ${res.status} ${body.slice(0, 300)}`);
  return body ? JSON.parse(body) : null;
}

// ---- self-test ---------------------------------------------------------------------------------

function entryFor(crawler) {
  return fs.existsSync(path.join(CRAWLERS_DIR, crawler, 'src', 'main.mjs')) &&
    !fs.existsSync(path.join(CRAWLERS_DIR, crawler, 'dist', 'main.js'))
    ? path.join('src', 'main.mjs') : path.join('dist', 'main.js');
}

async function selfTest({ launchBrowser = true } = {}) {
  const checks = {};
  const major = Number(process.versions.node.split('.')[0]);
  checks.node = { ok: major >= 18, detail: `Node ${process.versions.node}${major >= 18 ? '' : ' (needs 18 or newer)'}` };

  const missing = [];
  for (const c of VERSION_INFO.crawlers || []) {
    if (!fs.existsSync(path.join(CRAWLERS_DIR, c, entryFor(c)))) missing.push(c);
  }
  const depsOk = fs.existsSync(path.join(CRAWLERS_DIR, 'node_modules', 'crawlee', 'package.json'));
  checks.crawlers = {
    ok: missing.length === 0 && depsOk,
    detail: missing.length ? `missing: ${missing.join(', ')}` : depsOk ? `${(VERSION_INFO.crawlers || []).length} crawlers present`
      : 'crawler libraries (node_modules) are missing',
  };

  process.env.PLAYWRIGHT_BROWSERS_PATH = BROWSERS_DIR;
  try {
    const { chromium } = createRequire(path.join(CRAWLERS_DIR, 'package.json'))('playwright');
    const exe = chromium.executablePath();
    if (!fs.existsSync(exe)) {
      checks.browser = { ok: false, detail: 'the headless browser is not installed' };
    } else if (!launchBrowser) {
      checks.browser = { ok: true, detail: 'browser present' };
    } else {
      const b = await chromium.launch({ headless: true });
      const version = b.version();
      await b.close();
      checks.browser = { ok: true, detail: `Chromium ${version} starts` };
    }
  } catch (e) {
    checks.browser = { ok: false, detail: `browser check failed: ${String(e.message).split('\n')[0].slice(0, 200)}` };
  }
  return { at: new Date().toISOString(), checks, ok: Object.values(checks).every((c) => c.ok) };
}

function printReport(report) {
  for (const [name, c] of Object.entries(report.checks)) console.log(`  ${c.ok ? '[ ok ]' : '[FAIL]'} ${name}: ${c.detail}`);
}

// ---- running a task ----------------------------------------------------------------------------

function killTree(child) {
  try {
    if (process.platform === 'win32') spawnSync('taskkill', ['/F', '/T', '/PID', String(child.pid)], { windowsHide: true });
    else process.kill(-child.pid, 'SIGKILL');
  } catch { /* already gone */ }
  try { child.kill('SIGKILL'); } catch { /* already gone */ }
}

function readDataset(dir) {
  if (!fs.existsSync(dir)) return [];
  const rows = [];
  for (const f of fs.readdirSync(dir).filter((n) => n.endsWith('.json')).sort()) {
    try { rows.push(JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'))); } catch { /* skip a torn file */ }
  }
  return rows;
}

function safeEnv(requested) {
  const env = {};
  for (const [k, v] of Object.entries(requested || {})) {
    if (ENV_ALLOWLIST.has(k)) env[k] = String(v);
  }
  return env;
}

function runProcess(args, cwd, env, timeoutSec) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, args, {
      cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], detached: process.platform !== 'win32',
    });
    let out = '';
    const keep = (d) => { out = (out + d.toString('utf8')).slice(-4000); };
    child.stdout.on('data', keep);
    child.stderr.on('data', keep);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; killTree(child); }, timeoutSec * 1000);
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out: String(e), timedOut }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, out, timedOut }); });
  });
}

async function runTask(task) {
  const { crawler, kind, request } = task;
  const known = new Set(VERSION_INFO.crawlers || []);
  if (!/^[a-z0-9-]+$/.test(crawler) || !known.has(crawler)) throw new Error(`unknown crawler '${crawler}'`);
  const dir = path.join(CRAWLERS_DIR, crawler);
  if (!fs.existsSync(dir)) throw new Error(`crawler folder missing: ${crawler}`);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `vienna_${crawler.replace(/-/g, '_')}_`));
  try {
    const env = {
      ...process.env,
      PLAYWRIGHT_BROWSERS_PATH: BROWSERS_DIR,
      CRAWLEE_STORAGE_DIR: path.join(tmp, 'storage'),
      ...safeEnv(request.env),
    };
    const extra = (request.extra_args || []).map(String);
    let args;
    if (kind === 'csv') {
      const input = path.join(tmp, 'input.csv');
      fs.writeFileSync(input, request.input_csv, 'utf8');
      args = [path.join('dist', 'main.js'), input, ...extra];
    } else if (kind === 'node') {
      const entry = String(request.entry || '');
      const resolved = path.resolve(dir, entry);
      if (!entry || path.isAbsolute(entry) || !resolved.startsWith(dir + path.sep)) throw new Error(`bad entry '${entry}'`);
      args = [entry, ...(request.cli_args || []).map(String)];
    } else {
      throw new Error(`unknown task kind '${kind}'`);
    }

    const timeout = Math.max(10, Math.min(Number(request.timeout) || 90, 900));
    const { code, out, timedOut } = await runProcess(args, dir, env, timeout);
    if (timedOut) throw new Error(`${crawler} timed out after ${timeout}s`);
    if (code !== 0) throw new Error(`${crawler} exited ${code}: ${out.slice(-1500)}`);
    return readDataset(path.join(tmp, 'storage', 'datasets', 'default'));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

// ---- main loop -----------------------------------------------------------------------------------

function acquireSingleInstance() {
  // A second copy (e.g. the startup entry plus a manual launch) would only fight over tasks;
  // the pid file makes the newer one exit quietly.
  try {
    const pid = Number(fs.readFileSync(LOCK_PORT_FILE, 'utf8'));
    if (pid && pid !== process.pid) {
      try { process.kill(pid, 0); return false; } catch { /* stale pid file */ }
    }
  } catch { /* no pid file */ }
  fs.writeFileSync(LOCK_PORT_FILE, String(process.pid));
  return true;
}

async function main() {
  trimLog();
  if (process.argv.includes('--selftest')) {
    console.log(`Checking the installation (build ${VERSION_INFO.build})...`);
    const report = await selfTest();
    printReport(report);
    writeStatus({ selftest: report });
    process.exit(report.ok ? 0 : 1);
  }

  if (!acquireSingleInstance()) { log('another worker is already running — exiting'); process.exit(0); }
  const cleanup = () => { try { fs.rmSync(LOCK_PORT_FILE, { force: true }); } catch { /* ignore */ } };
  process.on('exit', cleanup);
  process.on('SIGINT', () => process.exit(0));
  process.on('SIGTERM', () => process.exit(0));

  const maxParallel = Math.max(1, Math.min(Number(CONFIG.maxParallel) || 3, 6));
  let running = 0;
  let selfTestReport = await selfTest();
  let selfTestAt = Date.now();
  log(`worker ${VERSION_INFO.version} (build ${VERSION_INFO.build}) starting as '${CONFIG.name}'; self-test ${selfTestReport.ok ? 'passed' : 'FAILED'}`);

  const info = () => ({
    protocol: PROTOCOL, version: VERSION_INFO.version, build: VERSION_INFO.build,
    node: process.versions.node, os: `${os.platform()} ${os.release()}`, host: os.hostname(),
    busy: running, max_parallel: maxParallel, checks: selfTestReport.checks, selftest_at: selfTestReport.at,
  });

  let lastBeat = 0;
  let connected = false;
  const beat = async () => {
    const r = await rpc('vienna_worker_heartbeat', { p_token: CONFIG.token, p_info: info() });
    lastBeat = Date.now();
    if (!r || r.ok === false) {
      log(`server refused this worker (${r && r.reason}); it was removed in the app — stopping`);
      writeStatus({ state: 'revoked' });
      process.exit(0);
    }
    if (!connected) { connected = true; log('connected to Vienna'); }
    writeStatus({ state: 'connected', lastHeartbeat: new Date().toISOString(), selftest: selfTestReport });
  };

  const execute = async (task) => {
    running += 1;
    log(`task ${task.id}: ${task.crawler} started`);
    try {
      const rows = await runTask(task);
      await rpc('vienna_worker_complete', { p_token: CONFIG.token, p_task_id: task.id, p_ok: true, p_result: { rows }, p_error: null });
      log(`task ${task.id}: ${task.crawler} done (${rows.length} rows)`);
    } catch (e) {
      log(`task ${task.id}: ${task.crawler} failed: ${String(e.message).slice(0, 300)}`);
      try {
        await rpc('vienna_worker_complete', { p_token: CONFIG.token, p_task_id: task.id, p_ok: false, p_result: null, p_error: String(e.message).slice(0, 3900) });
      } catch (e2) { log(`task ${task.id}: could not report the failure: ${e2.message}`); }
    } finally {
      running -= 1;
    }
  };

  for (;;) {
    try {
      if (Date.now() - lastBeat >= HEARTBEAT_MS) await beat();
      if (Date.now() - selfTestAt >= SELFTEST_EVERY_MS && running === 0) {
        selfTestReport = await selfTest();
        selfTestAt = Date.now();
        lastBeat = 0; // report the fresh result straight away
      }
      while (running < maxParallel) {
        const r = await rpc('vienna_worker_claim', { p_token: CONFIG.token });
        if (!r || r.ok === false) throw new Error(`claim refused: ${r && r.reason}`);
        if (!r.task) break;
        void execute(r.task);
      }
    } catch (e) {
      if (connected) log(`connection problem (will keep retrying): ${String(e.message).slice(0, 200)}`);
      connected = false;
    }
    await new Promise((r) => setTimeout(r, CLAIM_MS));
  }
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
