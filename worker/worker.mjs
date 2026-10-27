// Vienna Crawler Worker — runs the Vienna crawlers on this computer when the Vienna app asks.
//
// It holds no database password. It only knows the worker_shim/ service's URL and its own
// token, and talks to three token-checked functions (worker/supabase_rpc.sql on the server)
// through that shim:
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
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createSiteEvidence, tailLines } from './site_evidence.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = process.env.VIENNA_WORKER_HOME ? path.resolve(process.env.VIENNA_WORKER_HOME) : path.resolve(HERE, '..');
const CRAWLERS_DIR = path.join(ROOT, 'crawlers');
// A plain Python tool, not one of the Node crawlers above — bundled and dispatched
// separately (see executeWaybackTimeline below) because it runs for hours, not minutes,
// and must never occupy one of the maxParallel crawl slots or hit runTask's 900s cap.
// Kept nested under tools/ in the bundle too, matching the Scraper repo's own layout
// exactly, since the tool's source imports as `tools.wayback_timeline.*` (absolute,
// matching how it's run and tested there) - flattening the path here would break them.
const WAYBACK_DIR = path.join(ROOT, 'tools', 'wayback_timeline');
const WAYBACK_OUTPUT_DIR = path.join(os.homedir(), 'WaybackTimelineOutput');
const BROWSERS_DIR = path.join(ROOT, 'browsers');
const LOG_FILE = path.join(ROOT, 'worker.log');
const STATUS_FILE = path.join(ROOT, 'status.json');
const UPDATE_FAILURE_FILE = path.join(ROOT, 'update-failure.json');
const LOCK_PORT_FILE = path.join(ROOT, 'worker.pid');

const HEARTBEAT_MS = 15_000;
const CLAIM_MS = 2_000;
const SELFTEST_EVERY_MS = 30 * 60_000;
const RPC_TIMEOUT_MS = 25_000;
const UPDATE_CHECK_MS = 5 * 60_000;
const PROTOCOL = 1;

// Crawler subprocesses may only receive these settings from the app. The task queue is
// trusted, but a worker should still never let it inject things like NODE_OPTIONS or PATH.
const ENV_ALLOWLIST = new Set([
  'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL', 'WAYBACK_DELAY_MS', 'SEARCH_PROVIDER',
  'NEWSAPI_KEY', 'ANTHROPIC_API_KEY', 'BUILTWITH_API_KEY', 'LINKEDIN_LI_AT',
  // Wall-clock the crawler may use before it must hand back what it has (set a little under the
  // kill timeout), and the optional second LLM provider used when the first is rate-limited.
  'SOFT_DEADLINE_SECONDS', 'LLM_FALLBACK_API_KEY', 'LLM_FALLBACK_BASE_URL', 'LLM_FALLBACK_MODEL',
  'LLM_REASONING_EFFORT',
  // Third through fifth numbered fallbacks (config.py CRAWLER_LLM_EXTRA_FALLBACK_SUFFIXES)
  'LLM_FALLBACK2_API_KEY', 'LLM_FALLBACK2_BASE_URL', 'LLM_FALLBACK2_MODEL',
  'LLM_FALLBACK3_API_KEY', 'LLM_FALLBACK3_BASE_URL', 'LLM_FALLBACK3_MODEL',
  'LLM_FALLBACK4_API_KEY', 'LLM_FALLBACK4_BASE_URL', 'LLM_FALLBACK4_MODEL',
  'LLM_FALLBACK5_API_KEY', 'LLM_FALLBACK5_BASE_URL', 'LLM_FALLBACK5_MODEL',
  // digital-maturity-crawler's visual-assessment phase (2026-10-08) — a separate provider
  // namespace from LLM_* above, see config.py's CRAWLER_VISION_LLM_* docstring. Same
  // primary + fallbacks coverage as the LLM_* entries; without these, that phase's calls
  // get silently stripped to nothing on a remote worker and visual_assessment reads not_found
  // for every company it runs there, with no error surfaced.
  'VISION_LLM_API_KEY', 'VISION_LLM_BASE_URL', 'VISION_LLM_MODEL',
  'VISION_LLM_FALLBACK_API_KEY', 'VISION_LLM_FALLBACK_BASE_URL', 'VISION_LLM_FALLBACK_MODEL',
  'VISION_LLM_FALLBACK2_API_KEY', 'VISION_LLM_FALLBACK2_BASE_URL', 'VISION_LLM_FALLBACK2_MODEL',
  'VISION_LLM_FALLBACK3_API_KEY', 'VISION_LLM_FALLBACK3_BASE_URL', 'VISION_LLM_FALLBACK3_MODEL',
  'VISION_LLM_FALLBACK4_API_KEY', 'VISION_LLM_FALLBACK4_BASE_URL', 'VISION_LLM_FALLBACK4_MODEL',
  'VISION_LLM_FALLBACK5_API_KEY', 'VISION_LLM_FALLBACK5_BASE_URL', 'VISION_LLM_FALLBACK5_MODEL',
  // Subprocess limits forwarded by scrapers/*.py
  'MAX_PAGES_PER_DOMAIN', 'LLM_CONCURRENCY', 'MAX_DETAIL_PAGES',
  // Deep-intelligence multi-provider pool (ticket I04): INTEL_LLM_<1..10>_*
  ...Array.from({ length: 10 }, (_, i) => i + 1).flatMap((n) => [
    `INTEL_LLM_${n}_NAME`, `INTEL_LLM_${n}_API_KEY`, `INTEL_LLM_${n}_BASE_URL`,
    `INTEL_LLM_${n}_MODEL`, `INTEL_LLM_${n}_TIER`, `INTEL_LLM_${n}_TASKS`,
    `INTEL_LLM_${n}_TPM`, `INTEL_LLM_${n}_RPM`,
  ]),
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

function removeRetiredInstallations() {
  const parent = path.dirname(ROOT);
  const prefix = `${path.basename(ROOT)}-retired-`;
  try {
    for (const name of fs.readdirSync(parent)) {
      if (name.startsWith(prefix)) {
        // The updater still needs an uncommitted backup for rollback. It marks a
        // leftover folder only after the replacement has connected and passed tests.
        if (!fs.existsSync(path.join(parent, name, '.vienna-update-complete'))) continue;
        try { fs.rmSync(path.join(parent, name), { recursive: true, force: true }); }
        catch (e) { log(`could not yet remove retired installation '${name}': ${e.message}`); }
      }
    }
  } catch { /* cleanup is best effort and must never stop the worker */ }
}

function writeStatus(patch) {
  let cur = {};
  try { cur = readJson(STATUS_FILE); } catch { /* first write */ }
  try { fs.writeFileSync(STATUS_FILE, JSON.stringify({ ...cur, ...patch }, null, 2)); } catch { /* best effort */ }
}

function updateFailure() {
  try { return readJson(UPDATE_FAILURE_FILE); } catch { return null; }
}

function recordUpdateFailure(message) {
  const failure = { message: String(message).slice(0, 300), at: new Date().toISOString() };
  try { fs.writeFileSync(UPDATE_FAILURE_FILE, JSON.stringify(failure)); } catch { /* best effort */ }
  writeStatus({ state: 'connected', updateError: failure.message });
}

// ---- talking to the server -----------------------------------------------------------------

async function rpc(fn, args) {
  const res = await fetch(`${CONFIG.shimUrl.replace(/\/$/, '')}/rpc/${fn}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args), signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`${fn} -> HTTP ${res.status} ${body.slice(0, 300)}`);
  return body ? JSON.parse(body) : null;
}

async function fetchJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(RPC_TIMEOUT_MS) });
  const body = await res.text();
  if (!res.ok) throw new Error(`HTTP ${res.status} ${body.slice(0, 300)}`);
  return JSON.parse(body);
}

async function startUpdate(manifest, workerInfo) {
  const reportProgress = async (progress) => {
    writeStatus({ state: 'updating', updateProgress: progress });
    try {
      await rpc('vienna_worker_heartbeat', { p_token: CONFIG.token, p_info: {
        ...workerInfo, update_state: 'updating', target_build: manifest.build,
        update_progress: progress,
      } });
    } catch (e) { log(`could not report update progress: ${String(e.message).slice(0, 200)}`); }
  };
  await reportProgress({ stage: 'downloading', label: 'Downloading crawler update', percent: 0, downloaded_bytes: 0, total_bytes: null });
  const download = await fetch(manifest.download_url, { signal: AbortSignal.timeout(120_000) });
  if (!download.ok) throw new Error(`update download -> HTTP ${download.status}`);
  const totalBytes = Number(download.headers.get('content-length')) || Number(manifest.size_bytes) || null;
  const reader = download.body?.getReader();
  if (!reader) throw new Error('update download returned no response body');
  const chunks = [];
  let downloadedBytes = 0;
  let lastReported = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(Buffer.from(value));
    downloadedBytes += value.byteLength;
    if (downloadedBytes > 100 * 1024 * 1024) throw new Error('update bundle is unexpectedly large');
    if (Date.now() - lastReported >= 700) {
      const percent = totalBytes ? Math.min(29, Math.floor(downloadedBytes / totalBytes * 29)) : null;
      await reportProgress({ stage: 'downloading', label: 'Downloading crawler update', percent,
        downloaded_bytes: downloadedBytes, total_bytes: totalBytes });
      lastReported = Date.now();
    }
  }
  const bytes = Buffer.concat(chunks);
  if (bytes.length > 100 * 1024 * 1024) throw new Error('update bundle is unexpectedly large');
  await reportProgress({ stage: 'verifying', label: 'Checking downloaded update', percent: 30,
    downloaded_bytes: bytes.length, total_bytes: totalBytes || bytes.length });
  const actual = crypto.createHash('sha256').update(bytes).digest('hex');
  if (actual !== manifest.sha256) throw new Error('update bundle failed checksum verification');

  const nonce = crypto.randomUUID();
  const zipPath = path.join(os.tmpdir(), `vienna-update-${nonce}.zip`);
  const updaterPath = path.join(os.tmpdir(), `vienna-update-${nonce}.ps1`);
  fs.writeFileSync(zipPath, bytes);
  // Extract update.ps1 from the bundle we just downloaded (the version being updated TO),
  // not from this installation's own worker/update.ps1 (the version being updated FROM). A
  // fix to the updater script itself would otherwise never take effect: every update would
  // keep re-running whichever copy happened to ship with whatever was already installed,
  // forever - see docs/CRAWLER_ROADMAP.md T23. Falls back to the installed copy only if the
  // bundle is somehow missing that entry, so a bad extraction doesn't block the update outright.
  const extract = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', `
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead('${zipPath}')
    try {
      $entry = $zip.GetEntry('worker/update.ps1')
      if (-not $entry) { throw 'worker/update.ps1 not found in bundle' }
      [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, '${updaterPath}', $true)
    } finally { $zip.Dispose() }
  `], { windowsHide: true });
  if (extract.status !== 0) {
    log(`could not extract update.ps1 from the new bundle (${extract.stderr?.toString().trim()}); falling back to the installed copy`);
    fs.copyFileSync(path.join(HERE, 'update.ps1'), updaterPath);
  }
  writeStatus({ state: 'updating', fromBuild: VERSION_INFO.build, toBuild: manifest.build,
    startedAt: new Date().toISOString(), updateProgress: { stage: 'starting', label: 'Starting installer', percent: 32 } });
  await rpc('vienna_worker_heartbeat', { p_token: CONFIG.token, p_info: {
    ...workerInfo, update_state: 'updating', target_build: manifest.build,
    update_progress: { stage: 'starting', label: 'Starting installer', percent: 32 },
  } });
  log(`update ${VERSION_INFO.build} -> ${manifest.build} downloaded and verified; handing over to updater`);

  // Neither plain spawn(..., {detached:true}) nor routing it through cmd.exe's own
  // `start` survives this process's own exit reliably on this platform - confirmed by
  // direct testing both ways (the exact symptom both times: the updater silently killed
  // before it ever wrote its first log line). Tight process-tree polling during a real
  // update caught it precisely: this process (node.exe) has its own hidden console, and
  // whatever `start` launches stays attached to THAT console rather than getting a truly
  // independent one - so it dies with the console when this process exits, regardless of
  // detached/new-process-group flags, which only govern process-group membership, not
  // console ownership.
  //
  // First fix: PowerShell's own Start-Process (no -Wait), called via spawnSync so this
  // process waits for it to confirm the new process exists before exiting - the same
  // mechanism Start-InstalledWorker (below) already relies on for launching the final
  // worker.mjs after a completed update, which persists fine across logouts. Tested sound
  // in isolation (console-less VBS launch, matching how start-worker.vbs launches this
  // process) - but STILL died within ~1s in the one real deployment it was tried against,
  // confirmed by the same tight process-tree polling that found the console-ownership bug
  // above. Start-Process still goes through this process's own console/job chain somehow;
  // it's not the clean break it looks like.
  //
  // Second fix: WMI's Win32_Process.Create. This doesn't fork off this process's tree at
  // all - the resulting process's parent is the WMI provider host (WmiPrvSE.exe), not this
  // process, so there's nothing here for it to inherit or die with. Verified the same way
  // as both previous attempts: console-less isolated launch, 8s delay, parent long gone -
  // survived. Same root-cause methodology as the three other updater bugs already
  // documented under docs/CRAWLER_ROADMAP.md T23: run it for real and watch what actually
  // happens, don't assume from one isolated pass - that's what sank the first two fixes.
  const psQuote = (s) => `'${String(s).replace(/'/g, "''")}'`;
  const winArgQuote = (arg) => {
    let result = '"', backslashes = 0;
    for (const ch of String(arg)) {
      if (ch === '\\') { backslashes++; continue; }
      if (ch === '"') { result += '\\'.repeat(backslashes * 2 + 1) + '"'; backslashes = 0; continue; }
      result += '\\'.repeat(backslashes) + ch;
      backslashes = 0;
    }
    return result + '\\'.repeat(backslashes * 2) + '"';
  };
  const updaterArgs = [
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', updaterPath,
    '-Dir', ROOT, '-ZipPath', zipPath, '-ExpectedBuild', manifest.build,
    '-ParentPid', String(process.pid), '-WorkerInfoBase64', Buffer.from(JSON.stringify(workerInfo)).toString('base64'),
  ];
  const updaterCommandLine = ['powershell.exe', ...updaterArgs].map(winArgQuote).join(' ');
  const launchScript = `$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=${psQuote(updaterCommandLine)}}; if ($r.ReturnValue -ne 0) { Write-Error "WMI process creation failed with code $($r.ReturnValue)"; exit 1 }; Write-Output $r.ProcessId`;
  const launch = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', launchScript],
    { windowsHide: true, encoding: 'utf8' });
  if (launch.status !== 0) {
    throw new Error(`could not launch the updater: ${(launch.stderr?.trim() || launch.error?.message || `exit ${launch.status}`)}`);
  }
  const updaterPid = Number(launch.stdout.trim());
  if (!Number.isInteger(updaterPid) || updaterPid <= 0) {
    throw new Error(`updater launch returned no process id: ${launch.stdout.trim().slice(0, 100)}`);
  }
  // update.ps1 waits for this worker to exit before replacing any files. If it
  // disappears during that wait, it never started correctly: keep serving
  // crawls and retry later instead of leaving the PC permanently offline.
  await new Promise((resolve) => setTimeout(resolve, 5_000));
  try { process.kill(updaterPid, 0); }
  catch { throw new Error('the updater exited before taking over; the worker is still running'); }
  process.exit(0);
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

// ---- wayback_timeline: a Python tool, dispatched fire-and-forget, never awaited here ------------
//
// Unlike a crawl task, "run" isn't the unit of work the server asks for — "start" and "stop"
// are. The Python process manages its own lifecycle (resumable, RAM-gated, stoppable via its
// own STOP file; see tools/wayback_timeline/ram_guard.py in the Scraper repo this is bundled
// from). The worker's job is just: act on the request immediately, report back immediately,
// and separately surface its live status on every heartbeat — never block a claim slot on it.

// The Python process's own stdout/stderr used to go to stdio:'ignore' - fine while it's
// running, but when it refuses to start at all (e.g. the RAM gate in ram_guard.py) or
// crashes on an unhandled exception, that reason vanished into nothing. The app showed a
// bare "Not running" with no way to tell "never tried" from "tried and refused" apart.
// Redirect to a log file instead and surface its last line in the heartbeat, so the one
// thing standing between "Start" and an actual run is visible from the app, not something
// that needs a live SSH session into this machine to diagnose.
function waybackTimelineLastLog() {
  try {
    const text = fs.readFileSync(path.join(WAYBACK_OUTPUT_DIR, 'run.log'), 'utf8');
    const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    return lines.length ? lines[lines.length - 1].slice(0, 300) : null;
  } catch { return null; }
}

let pythonCheckAt = 0;
let pythonCheck = null;
function waybackPythonStatus(force = false) {
  if (!force && pythonCheck && Date.now() - pythonCheckAt < 60_000) return pythonCheck;
  const check = spawnSync('python', ['--version'], { encoding: 'utf8', timeout: 5_000, windowsHide: true });
  pythonCheckAt = Date.now();
  pythonCheck = check.status === 0
    ? { ok: true }
    : { ok: false, detail: 'Python is not available on this computer. Install Python 3 and restart the worker.' };
  return pythonCheck;
}

function waybackTimelineInfo() {
  const available = fs.existsSync(WAYBACK_DIR);
  try {
    const pidFile = path.join(WAYBACK_OUTPUT_DIR, 'running.pid');
    let running = false, pid = null;
    if (fs.existsSync(pidFile)) {
      pid = Number(fs.readFileSync(pidFile, 'utf8').trim());
      try { process.kill(pid, 0); running = true; } catch { running = false; }
    }
    let done = 0, total = 0, hits = 0;
    const statePath = path.join(WAYBACK_OUTPUT_DIR, 'state.json');
    if (fs.existsSync(statePath)) {
      const entries = Object.values(readJson(statePath));
      total = entries.length;
      done = entries.filter((e) => e.done).length;
      hits = entries.filter((e) => e.first_hit).length;
    }
    const python = available ? waybackPythonStatus() : null;
    return { available, pythonReady: python?.ok ?? false, running, pid: running ? pid : null,
      done, total, hits, lastLog: waybackTimelineLastLog(),
      ...(python && !python.ok ? { error: python.detail } : {}) };
  } catch (e) {
    return { available, running: false, error: String(e.message).slice(0, 200) };
  }
}

async function startWaybackTimeline(task) {
  if (!fs.existsSync(WAYBACK_DIR)) throw new Error('wayback_timeline is not present in this install');
  if (waybackTimelineInfo().running) return { already_running: true };
  const python = waybackPythonStatus(true);
  if (!python.ok) throw new Error(python.detail);
  // The company list travels with the task request (same convention the existing
  // crawlers use for kind='csv' input), not a static bundled file - the app dispatching
  // this may have no filesystem in common with this machine at all, so the CSV the user
  // last edited in the panel is what's authoritative, always sent fresh at Start time.
  const inputCsv = String((task.request || {}).input_csv || '');
  if (!inputCsv.trim()) throw new Error('no company list in this request');
  fs.mkdirSync(WAYBACK_OUTPUT_DIR, { recursive: true });
  // A previous Stop must not immediately stop the next resumed run.
  fs.rmSync(path.join(WAYBACK_OUTPUT_DIR, 'STOP'), { force: true });
  fs.rmSync(path.join(WAYBACK_OUTPUT_DIR, 'running.pid'), { force: true });
  const companiesPath = path.join(WAYBACK_OUTPUT_DIR, 'companies.csv');
  fs.writeFileSync(companiesPath, inputCsv, 'utf8');
  const logFd = fs.openSync(path.join(WAYBACK_OUTPUT_DIR, 'run.log'), 'w');
  const child = spawn('python', [
    '-u', '-m', 'tools.wayback_timeline.timeline_builder', '--input', companiesPath,
  ], { cwd: ROOT, detached: true, stdio: ['ignore', logFd, logFd], windowsHide: true });
  fs.closeSync(logFd);
  await new Promise((resolve, reject) => {
    child.once('spawn', resolve);
    child.once('error', reject);
  });
  child.unref();
  // Python writes running.pid near the start of main(). A successful spawn only
  // proves Windows accepted the command; it does not prove the tool stayed up.
  const deadline = Date.now() + 10_000;
  let runningSince = null;
  while (Date.now() < deadline) {
    const info = waybackTimelineInfo();
    if (info.running && info.pid === child.pid) {
      runningSince ??= Date.now();
      if (Date.now() - runningSince >= 1_500) return { started: true, pid: child.pid };
    } else {
      runningSince = null;
    }
    if (child.exitCode !== null || child.signalCode !== null) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`wayback_timeline did not start: ${waybackTimelineLastLog() ||
    `Python exited with code ${child.exitCode ?? 'unknown'}`}`);
}

function stopWaybackTimeline() {
  fs.mkdirSync(WAYBACK_OUTPUT_DIR, { recursive: true });
  fs.writeFileSync(path.join(WAYBACK_OUTPUT_DIR, 'STOP'), 'requested via Vienna worker task');
  return { stop_requested: true };
}

async function executeWaybackTimeline(task) {
  log(`task ${task.id}: wayback_timeline ${task.kind}`);
  let result, ok = true, error = null;
  try {
    if (task.kind === 'start') result = await startWaybackTimeline(task);
    else if (task.kind === 'stop') result = stopWaybackTimeline();
    else throw new Error(`unknown wayback_timeline kind '${task.kind}'`);
  } catch (e) {
    ok = false;
    error = String(e.message).slice(0, 3900);
    log(`task ${task.id}: wayback_timeline ${task.kind} failed: ${error}`);
  }
  try {
    await rpc('vienna_worker_complete', { p_token: CONFIG.token, p_task_id: task.id, p_ok: ok, p_result: result || null, p_error: error });
  } catch (e2) { log(`task ${task.id}: could not report the result: ${e2.message}`); }
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

  const maxParallel = Math.max(1, Math.min(Number(CONFIG.maxParallel) || 4, 8));
  let running = 0;
  let selfTestReport = await selfTest();
  let selfTestAt = Date.now();
  log(`worker ${VERSION_INFO.version} (build ${VERSION_INFO.build}) starting as '${CONFIG.name}'; self-test ${selfTestReport.ok ? 'passed' : 'FAILED'}`);

  // The local-model reader of companies' own websites (T26): runs for hours or days under its own
  // supervisor, reports to the database through the same shim, and survives updates and reboots.
  const siteEvidence = createSiteEvidence({
    root: ROOT, token: CONFIG.token, rpc, log,
    workerInfo: () => ({ build: VERSION_INFO.build, version: VERSION_INFO.version, host: os.hostname(), worker_name: CONFIG.name }),
    workerDiagnostics: () => ({
      worker_log_tail: tailLines(LOG_FILE, 300), status: (() => { try { return readJson(STATUS_FILE); } catch { return null; } })(),
      update_failure: updateFailure(), self_test: selfTestReport, node: process.versions.node, os: `${os.platform()} ${os.release()}`,
    }),
  });

  const info = () => {
    const failure = updateFailure();
    return {
      protocol: PROTOCOL, version: VERSION_INFO.version, build: VERSION_INFO.build,
      node: process.versions.node, os: `${os.platform()} ${os.release()}`, host: os.hostname(),
      busy: running, max_parallel: maxParallel, checks: selfTestReport.checks, selftest_at: selfTestReport.at,
      auto_update: true, wayback_timeline: waybackTimelineInfo(), site_evidence: siteEvidence.info(),
      ...(failure ? { update_error: failure } : {}),
    };
  };

  let lastBeat = 0;
  let lastUpdateCheck = 0;
  let lastUpdateError = '';
  let waybackActionChain = Promise.resolve();
  let connected = false;
  let lastConnectionError = '';
  const beat = async () => {
    const r = await rpc('vienna_worker_heartbeat', { p_token: CONFIG.token, p_info: info() });
    lastBeat = Date.now();
    if (!r || r.ok === false) {
      log(`server refused this worker (${r && r.reason}); it was removed in the app — stopping`);
      writeStatus({ state: 'revoked' });
      process.exit(0);
    }
    if (!connected) {
      connected = true;
      lastConnectionError = '';
      log('connected to Vienna');
      removeRetiredInstallations();
    }
    writeStatus({ state: 'connected', lastHeartbeat: new Date().toISOString(), selftest: selfTestReport });
  };

  // Start / stop / diagnostics / resync for the site evidence reader. Like the Wayback actions these are
  // fire-and-forget: the run itself lives in its own process and reports through the reporter, so the task
  // completes at once and never occupies one of the crawl slots.
  const executeSiteEvidence = async (task) => {
    log(`task ${task.id}: site_evidence ${task.kind}`);
    let result = null;
    let ok = true;
    let error = null;
    try { result = await siteEvidence.handleTask(task); }
    catch (e) { ok = false; error = String(e.message).slice(0, 3900); log(`task ${task.id}: site_evidence ${task.kind} failed: ${error}`); }
    try {
      await rpc('vienna_worker_complete', { p_token: CONFIG.token, p_task_id: task.id, p_ok: ok, p_result: result, p_error: error });
    } catch (e2) { log(`task ${task.id}: could not report the result: ${e2.message}`); }
  };

  const execute = async (task) => {
    if (task.crawler === 'site_evidence') { void executeSiteEvidence(task); return; }
    if (task.crawler === 'wayback_timeline') {
      // Start/Stop tasks can arrive together. Process them in claim order so a
      // second Start cannot race the first process's PID file.
      waybackActionChain = waybackActionChain.then(() => executeWaybackTimeline(task));
      return;
    }
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
    void siteEvidence.tick();   // never throws and never blocks: keeps the reader alive and reports its results
    try {
      if (Date.now() - lastBeat >= HEARTBEAT_MS) await beat();
      if (Date.now() - selfTestAt >= SELFTEST_EVERY_MS && running === 0) {
        selfTestReport = await selfTest();
        selfTestAt = Date.now();
        lastBeat = 0; // report the fresh result straight away
      }
      if (running === 0 && Date.now() - lastUpdateCheck >= UPDATE_CHECK_MS) {
        lastUpdateCheck = Date.now();
        try {
          const base = CONFIG.shimUrl.replace(/\/$/, '');
          const manifest = await fetchJson(`${base}/update/latest`);
          if (!manifest.build || !manifest.sha256 || !manifest.download_url) {
            throw new Error('update service returned an incomplete manifest');
          }
          if (manifest.build !== VERSION_INFO.build) {
            // The installation is about to be replaced: a running reader is stopped cleanly first (its
            // progress is saved per company) and is resumed automatically afterwards, by this worker if
            // the update fails and by the new one if it succeeds. A recent pause defers the update.
            if (await siteEvidence.pauseForUpdate()) await startUpdate(manifest, info());
          }
          fs.rmSync(UPDATE_FAILURE_FILE, { force: true });
          lastUpdateError = '';
        } catch (e) {
          const message = String(e.message).slice(0, 300);
          if (message !== lastUpdateError) log(`update check failed (normal work continues): ${message}`);
          lastUpdateError = message;
          recordUpdateFailure(message);
          lastBeat = 0; // replace the temporary "updating" heartbeat immediately
        }
      }
      while (running < maxParallel) {
        const r = await rpc('vienna_worker_claim', { p_token: CONFIG.token });
        if (!r || r.ok === false) throw new Error(`claim refused: ${r && r.reason}`);
        if (!r.task) break;
        void execute(r.task);
      }
    } catch (e) {
      const message = String(e.message).slice(0, 300);
      // Log the first failure as well as any change in the failure. Previously initial
      // failures were completely invisible, which made a bad or stale configuration look
      // like an ordinary offline computer forever.
      if (connected || message !== lastConnectionError) {
        log(`connection problem (will keep retrying): ${message}`);
        lastConnectionError = message;
      }
      writeStatus({ state: 'retrying', lastError: message, lastAttempt: new Date().toISOString() });
      connected = false;
    }
    await new Promise((r) => setTimeout(r, CLAIM_MS));
  }
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
