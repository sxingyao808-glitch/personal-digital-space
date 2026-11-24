'use strict';

/**
 * Lifecycle adversarial checks for the desktop shell.
 *
 *   1. single instance: a second launch must not start a second backend
 *   2. forced shell kill: the backend must exit by itself (parent-pipe death)
 *   3. restart after a forced kill: the same data directory must open again
 *
 * Usage:
 *   node scripts/verify-lifecycle.js --exe <PersonalDigitalSpace.exe|electron.exe>
 *        --app <desktop dir, empty for packaged> --data-dir <dir>
 *        --user-data-dir <dir> [--packaged] [--process-image archive-backend.exe]
 */

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    switch (token) {
      case '--exe':
        args.exe = argv[++index];
        break;
      case '--app':
        args.app = argv[++index];
        break;
      case '--data-dir':
        args.dataDir = argv[++index];
        break;
      case '--user-data-dir':
        args.userDataDir = argv[++index];
        break;
      case '--process-image':
        args.processImage = argv[++index];
        break;
      case '--debug-port':
        args.debugPort = Number(argv[++index]);
        break;
      case '--packaged':
        args.packaged = true;
        break;
      default:
        throw new Error(`unknown argument: ${token}`);
    }
  }
  for (const required of ['exe', 'app', 'dataDir', 'userDataDir']) {
    if (!args[required]) throw new Error(`missing --${required}`);
  }
  return args;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function findBackendPids(dataDir, imageName) {
  const script =
    `Get-CimInstance Win32_Process -Filter "Name='${imageName}'" | ` +
    `Where-Object { $_.CommandLine -and $_.CommandLine.Contains('${dataDir}') } | ` +
    `ForEach-Object { $_.ProcessId }`;
  const result = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
    windowsHide: true,
    encoding: 'utf8',
  });
  return (result.stdout || '')
    .split(/\r?\n/)
    .map((line) => Number(line.trim()))
    .filter((value) => Number.isInteger(value) && value > 0);
}

async function launch(args, debugPort) {
  const launchArgs = args.packaged
    ? [`--user-data-dir=${path.resolve(args.userDataDir)}`, `--remote-debugging-port=${debugPort}`]
    : [
        path.resolve(args.app),
        `--user-data-dir=${path.resolve(args.userDataDir)}`,
        `--remote-debugging-port=${debugPort}`,
      ];
  const child = spawn(path.resolve(args.exe), launchArgs, {
    cwd: path.resolve(args.app),
    env: { ...process.env, ARCHIVE_DATA_DIR: path.resolve(args.dataDir) },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const exit = new Promise((resolve) => child.once('exit', (code) => resolve(code)));
  return { child, exit };
}

async function waitFor(check, timeoutMs, intervalMs = 300) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await delay(intervalMs);
  }
  return false;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const image = args.processImage || 'archive-backend.exe';
  const dataDir = path.resolve(args.dataDir);
  const userDataDir = path.resolve(args.userDataDir);
  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(userDataDir, { recursive: true });

  const report = { checks: [] };
  const record = (name, ok, detail) => {
    report.checks.push({ name, ok, detail: detail === undefined ? '' : String(detail) });
    if (!ok) throw new Error(`check failed: ${name} (${detail})`);
  };

  let first = await launch(args, 9441);
  const started = await waitFor(() => findBackendPids(dataDir, image).length === 1, 60000);
  record('first_instance.backend_started', started);
  await delay(3000);
  const backendPid = findBackendPids(dataDir, image)[0];
  record('first_instance.single_backend', findBackendPids(dataDir, image).length === 1, backendPid);

  // Second launch: must hand focus back and exit without a second backend.
  const second = await launch(args, 9442);
  const secondExit = await Promise.race([second.exit, delay(20000).then(() => 'timeout')]);
  record('second_instance.exited', secondExit === 0, secondExit);
  await delay(1500);
  const afterSecond = findBackendPids(dataDir, image);
  record('second_instance.no_extra_backend', afterSecond.length === 1, afterSecond.join(','));
  record('second_instance.same_backend', afterSecond[0] === backendPid, afterSecond.join(','));

  // Force kill the shell only; the backend must notice its parent is gone.
  spawnSync('taskkill', ['/pid', String(first.child.pid), '/T', '/F'], {
    windowsHide: true,
    stdio: 'ignore',
  });
  const backendGone = await waitFor(() => findBackendPids(dataDir, image).length === 0, 20000);
  record('forced_kill.backend_exited', backendGone, findBackendPids(dataDir, image).join(','));
  const firstExit = await Promise.race([first.exit, delay(5000).then(() => 'timeout')]);
  report.firstShellExit = firstExit;

  // Restart on the same data directory: the lock must be free and data intact.
  first = await launch(args, 9443);
  const restarted = await waitFor(() => findBackendPids(dataDir, image).length === 1, 60000);
  record('restart.backend_started', restarted);
  record('restart.database_present', fs.existsSync(path.join(dataDir, 'archive.db')));

  // Normal close on the restarted instance.
  await delay(2000);
  const restartedPid = findBackendPids(dataDir, image)[0];
  const close = spawnSync(
    'powershell',
    [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$p = Get-Process -Id ${first.child.pid} -ErrorAction SilentlyContinue; if ($p) { $p.CloseMainWindow() | Out-Null }`,
    ],
    { windowsHide: true, encoding: 'utf8' }
  );
  void close;
  const shellGone = await waitFor(
    () => {
      try {
        process.kill(first.child.pid, 0);
        return false;
      } catch (error) {
        return true;
      }
    },
    25000
  );
  record('restart.shell_closed', shellGone);
  const stoppedAfterClose = await waitFor(
    () => {
      try {
        process.kill(restartedPid, 0);
        return false;
      } catch (error) {
        return true;
      }
    },
    20000
  );
  record('restart.backend_stopped', stoppedAfterClose, restartedPid);

  console.log(JSON.stringify({ ok: true, ...report }, null, 2));
}

main()
  .catch((error) => {
    console.error(JSON.stringify({ ok: false, error: String(error && error.message) }, null, 2));
    process.exitCode = 1;
  })
  .finally(() => {
    setTimeout(() => process.exit(process.exitCode || 0), 300);
  });
