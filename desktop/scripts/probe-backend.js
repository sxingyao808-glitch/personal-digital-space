'use strict';

/**
 * Headless verification of a packaged backend.
 *
 * Usage:
 *   node scripts/probe-backend.js --exe <path/to/archive-backend.exe> [--data-dir <dir>]
 *
 * It starts the sidecar on an OS-assigned loopback port, verifies the
 * readiness handshake, the served assets and the owner session, then stops it
 * through the controlled shutdown route. It never touches a real archive: the
 * data directory defaults to a fresh temporary folder that is removed after
 * the run (unless --keep is given).
 *
 * Exit code 0 only when every check passes.
 */

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const READY_PREFIX = 'ARCHIVE_DESKTOP_READY ';

let activeChild = null;

function parseArgs(argv) {
  const args = { keep: false };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--exe') args.exe = argv[++index];
    else if (token === '--data-dir') args.dataDir = argv[++index];
    else if (token === '--keep') args.keep = true;
    else if (token === '--timeout') args.timeout = Number(argv[++index]);
    else throw new Error(`unknown argument: ${token}`);
  }
  if (!args.exe) throw new Error('--exe is required');
  return args;
}

function get(port, requestPath, headers = {}) {
  return new Promise((resolve, reject) => {
    const request = http.get(
      { host: '127.0.0.1', port, path: requestPath, timeout: 8000, headers },
      (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => {
          body += chunk;
        });
        response.on('end', () => resolve({ status: response.statusCode, body, headers: response.headers }));
      }
    );
    request.on('timeout', () => request.destroy(new Error('request timeout')));
    request.on('error', reject);
  });
}

function post(port, requestPath, headers, body = '{}') {
  return new Promise((resolve, reject) => {
    const payload = Buffer.from(body, 'utf8');
    const request = http.request(
      {
        host: '127.0.0.1',
        port,
        path: requestPath,
        method: 'POST',
        timeout: 8000,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': payload.length,
          ...headers,
        },
      },
      (response) => {
        let text = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => {
          text += chunk;
        });
        response.on('end', () => resolve({ status: response.statusCode, body: text }));
      }
    );
    request.on('timeout', () => request.destroy(new Error('request timeout')));
    request.on('error', reject);
    request.end(payload);
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const timeout = args.timeout || 90000;
  const exe = path.resolve(args.exe);
  if (!fs.existsSync(exe)) throw new Error(`missing executable: ${exe}`);

  const createdTemp = !args.dataDir;
  const dataDir = args.dataDir
    ? path.resolve(args.dataDir)
    : fs.mkdtempSync(path.join(os.tmpdir(), 'archive-sidecar-'));
  const bootstrapToken = crypto.randomBytes(32).toString('hex');
  const controlToken = crypto.randomBytes(32).toString('hex');

  const report = {
    exe,
    dataDir,
    checks: [],
    handshake: null,
    exitCode: null,
  };
  const record = (name, ok, detail) => {
    report.checks.push({ name, ok, detail: detail === undefined ? '' : detail });
    if (!ok) throw new Error(`check failed: ${name}${detail ? ` (${detail})` : ''}`);
  };

  const child = spawn(
    exe,
    [
      '--desktop',
      '--data-dir',
      dataDir,
      '--port',
      '0',
      `--bootstrap-token=${bootstrapToken}`,
      `--control-token=${controlToken}`,
    ],
    { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } }
  );

  let stdout = '';
  let stderr = '';
  activeChild = child;
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    stdout += chunk;
  });
  child.stderr.on('data', (chunk) => {
    stderr += chunk;
  });

  const exited = new Promise((resolve) => {
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });

  let handshake = null;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const lines = stdout.split(/\r?\n/);
    const line = lines.find((entry) => entry.startsWith(READY_PREFIX));
    if (line) {
      handshake = JSON.parse(line.slice(READY_PREFIX.length));
      break;
    }
    if (child.exitCode !== null) break;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  if (!handshake) {
    child.kill();
    throw new Error(`no handshake within ${timeout}ms\nstdout:\n${stdout}\nstderr:\n${stderr}`);
  }
  report.handshake = handshake;
  record('handshake.prefix', true);
  record('handshake.port', Number.isInteger(handshake.port) && handshake.port > 1024, String(handshake.port));
  record('handshake.data_dir', path.resolve(handshake.data_dir) === path.resolve(dataDir), handshake.data_dir);
  record('handshake.schema_version', handshake.schema_version === 2, String(handshake.schema_version));
  record('handshake.no_token', !stdout.includes(bootstrapToken) && !stdout.includes(controlToken));

  const port = handshake.port;
  const health = await get(port, '/api/health');
  record('http.health', health.status === 200 && health.body.includes('"ok"'), String(health.status));

  const index = await get(port, '/auth?token=' + encodeURIComponent(bootstrapToken));
  record(
    'http.auth_redirect',
    index.status === 302 && (index.headers.location || '').endsWith('/'),
    `${index.status} ${index.headers.location}`
  );

  const sessionCookie = (index.headers['set-cookie'] || []).map((entry) => entry.split(';')[0]).join('; ');
  record('http.session_cookie', sessionCookie.length > 0);

  const home = await get(port, '/', { Cookie: sessionCookie });
  record('http.home_page', home.status === 200 && home.body.includes('Archive'), String(home.status));

  const api = await get(port, '/api/session', { Cookie: sessionCookie });
  record('http.session_owner', api.status === 200 && api.body.includes('"owner":true'), String(api.status));

  for (const asset of ['/static/app.js', '/static/styles.css']) {
    const response = await get(port, asset);
    record(`http.asset${asset}`, response.status === 200 && response.body.length > 1000, String(response.status));
  }

  const denied = await post(port, '/api/desktop/shutdown', { 'X-Desktop-Control': 'wrong' });
  record('shutdown.rejects_wrong_token', denied.status === 403, String(denied.status));

  const stopped = await post(port, '/api/desktop/shutdown', { 'X-Desktop-Control': controlToken });
  record('shutdown.accepted', stopped.status === 200, String(stopped.status));

  const exit = await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(() => resolve({ code: 'timeout' }), 20000)),
  ]);
  report.exitCode = exit.code;
  record('process.exited_cleanly', exit.code === 0, String(exit.code));

  const dbPath = path.join(dataDir, 'archive.db');
  record('data.database_created', fs.existsSync(dbPath));
  record('data.database_nonempty', fs.existsSync(dbPath) && fs.statSync(dbPath).size > 0);

  if (createdTemp && !args.keep) {
    fs.rmSync(dataDir, { recursive: true, force: true });
  }

  console.log(JSON.stringify({ ok: true, ...report }, null, 2));
}

main()
  .catch((error) => {
    console.error(JSON.stringify({ ok: false, error: String(error && error.message) }, null, 2));
    process.exitCode = 1;
  })
  .finally(() => {
    if (activeChild) {
      if (activeChild.exitCode === null) {
        try {
          activeChild.kill();
        } catch (error) {
          // Already gone.
        }
      }
      for (const stream of [activeChild.stdout, activeChild.stderr]) {
        if (stream) stream.destroy();
      }
    }
  });
