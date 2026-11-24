'use strict';

/**
 * Desktop shell for the local Archive.
 *
 * The window is a thin frame around the existing loopback Web UI:
 *   1. resolve a user-owned data directory,
 *   2. spawn the Python sidecar and wait for its readiness handshake,
 *   3. load the archive page into a sandboxed window,
 *   4. stop only that child process when the app quits.
 *
 * No business logic lives here.
 */

const { app, BrowserWindow, Menu, ipcMain, session, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const { BackendService, BackendError } = require('./backend');
const { resolveDataDir, CONFIG_FILENAME } = require('./config');

const PRODUCT_NAME = '个人数字空间';
const BACKGROUND = '#100e0c';
const APP_ID = 'com.personal.digitalspace.oss';
const REPO_ROOT = path.resolve(__dirname, '..', '..');
app.setName('Personal Digital Space OSS');

const SHELL_PRELOAD = path.join(__dirname, 'shell-preload.js');

const state = {
  backend: null,
  mainWindow: null,
  splashWindow: null,
  errorWindow: null,
  lastContext: null,
  dataDir: '',
  quitting: false,
  shuttingDown: false,
};

// ------------------------------------------------------------------ logging

function logDir() {
  return path.join(app.getPath('userData'), 'logs');
}

function logFile() {
  const directory = logDir();
  fs.mkdirSync(directory, { recursive: true });
  const stamp = new Date().toISOString().slice(0, 10);
  return path.join(directory, `app-${stamp}.log`);
}

function log(line) {
  try {
    fs.appendFileSync(logFile(), `[${new Date().toISOString()}] ${line}\n`, 'utf8');
  } catch (error) {
    // Logging must never break the app.
  }
}

// -------------------------------------------------------------- launch paths

function iconFile() {
  const candidate = app.isPackaged
    ? path.join(process.resourcesPath, 'icon.ico')
    : path.join(__dirname, '..', 'build', 'icon.ico');
  return fs.existsSync(candidate) ? candidate : null;
}

function backendLaunch() {
  // Prefer the bundled sidecar when it actually exists; fall back to the
  // interpreter. This keeps dev runs working even if the packaged/development
  // signal is ambiguous.
  const bundled = path.join(process.resourcesPath, 'backend', 'archive-backend.exe');
  if (fs.existsSync(bundled)) {
    return { executable: bundled, scriptArgs: [] };
  }
  return {
    executable: process.env.ARCHIVE_PYTHON || 'python',
    scriptArgs: [path.join(REPO_ROOT, 'run_app.py')],
  };
}

// Electron exposes ``appData`` (Roaming) but not ``localAppData``; derive the
// local location explicitly so the default data directory never roams.
function localAppDataDir() {
  if (process.env.LOCALAPPDATA) return process.env.LOCALAPPDATA;
  return path.join(app.getPath('home'), 'AppData', 'Local');
}

// ------------------------------------------------------------------ windows

function closeSplash() {
  if (state.splashWindow && !state.splashWindow.isDestroyed()) {
    state.splashWindow.destroy();
  }
  state.splashWindow = null;
}

function createSplash() {
  const splash = new BrowserWindow({
    width: 460,
    height: 320,
    frame: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    backgroundColor: BACKGROUND,
    title: PRODUCT_NAME,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  state.splashWindow = splash;
  splash.once('ready-to-show', () => {
    if (!splash.isDestroyed()) splash.show();
  });
  splash.on('closed', () => {
    if (state.splashWindow === splash) state.splashWindow = null;
  });
  splash.loadFile(path.join(__dirname, 'splash.html'));
}

function isAppUrl(target) {
  if (!state.backend || !state.backend.port) return false;
  try {
    const parsed = new URL(target);
    return (
      parsed.protocol === 'http:' &&
      parsed.hostname === '127.0.0.1' &&
      parsed.port === String(state.backend.port)
    );
  } catch (error) {
    return false;
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function createMainWindow(info) {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    backgroundColor: BACKGROUND,
    title: PRODUCT_NAME,
    autoHideMenuBar: true,
    ...(iconFile() ? { icon: iconFile() } : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  });
  state.mainWindow = win;

  // The window belongs to the application, not to the document title.
  win.on('page-title-updated', (event) => event.preventDefault());
  win.on('closed', () => {
    if (state.mainWindow === win) state.mainWindow = null;
  });

  win.webContents.on('did-fail-load', (event, code, description, url, isMainFrame) => {
    // -3 (ERR_ABORTED) is emitted by ordinary redirects; ignore it.
    if (!isMainFrame || code === -3) return;
    log(`页面加载失败：code=${code} url=${url} ${description}`);
    presentError(
      new BackendError('主界面无法加载', `本地页面加载失败：${description}`, {
        detail: `URL: ${url}\n错误码: ${code}`,
      }),
      'runtime'
    );
  });

  win.webContents.on('render-process-gone', (event, details) => {
    log(`渲染进程退出：${details.reason}`);
    presentError(
      new BackendError('界面已停止', '主界面渲染进程意外退出。', {
        detail: `原因: ${details.reason}\n退出码: ${details.exitCode}`,
      }),
      'runtime'
    );
  });

  const ready = new Promise((resolve) => win.once('ready-to-show', resolve));
  await win.loadURL(info.loginUrl);
  await Promise.race([ready, delay(2500)]);
  if (!win.isDestroyed()) {
    win.show();
    win.focus();
  }
  closeSplash();
}

function presentError(error, kind) {
  const context = {
    kind: kind || 'startup',
    title: (error && error.title) || '启动失败',
    message: (error && error.message) || String(error),
    detail: (error && error.detail) || '',
    dataDir: state.dataDir || (state.backend && state.backend.dataDir) || '',
    logDir: logDir(),
  };
  state.lastContext = context;
  log(`错误（${context.kind}）：${context.title} - ${context.message}`);

  if (state.errorWindow && !state.errorWindow.isDestroyed()) {
    state.errorWindow.destroy();
    state.errorWindow = null;
  }

  const win = new BrowserWindow({
    width: 640,
    height: 600,
    minWidth: 520,
    minHeight: 460,
    show: false,
    backgroundColor: BACKGROUND,
    title: PRODUCT_NAME,
    autoHideMenuBar: true,
    ...(iconFile() ? { icon: iconFile() } : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: SHELL_PRELOAD,
      devTools: !app.isPackaged,
    },
  });
  state.errorWindow = win;
  win.once('ready-to-show', () => {
    if (!win.isDestroyed()) win.show();
  });
  win.on('closed', () => {
    if (state.errorWindow === win) state.errorWindow = null;
  });
  win.loadFile(path.join(__dirname, 'error.html'));

  // Order matters: the error window now exists, so tearing down the rest
  // cannot trigger "window-all-closed" mid-transition.
  if (state.mainWindow && !state.mainWindow.isDestroyed()) {
    state.mainWindow.destroy();
  }
  state.mainWindow = null;
  closeSplash();
}

// ------------------------------------------------------------------ startup

function hardenSession() {
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback) => {
    callback(false);
  });
  session.defaultSession.setPermissionCheckHandler(() => false);
}

function isShellSender(event) {
  const sender = event.sender;
  return Boolean(
    (state.errorWindow && !state.errorWindow.isDestroyed() && sender === state.errorWindow.webContents) ||
      (state.splashWindow && !state.splashWindow.isDestroyed() && sender === state.splashWindow.webContents)
  );
}

function registerIpc() {
  ipcMain.handle('shell:context', (event) => {
    if (!isShellSender(event)) return null;
    return state.lastContext;
  });
  ipcMain.on('shell:retry', (event) => {
    if (!isShellSender(event)) return;
    relaunch();
  });
  ipcMain.on('shell:quit', (event) => {
    if (!isShellSender(event)) return;
    app.quit();
  });
}

async function relaunch() {
  state.shuttingDown = true;
  if (state.backend) {
    try {
      await state.backend.stop();
    } catch (error) {
      log(`停止后端失败：${error && error.message}`);
    }
  }
  app.relaunch();
  app.exit(0);
}

async function start() {
  Menu.setApplicationMenu(null);
  hardenSession();
  registerIpc();
  createSplash();

  try {
    log(
      `启动：isPackaged=${app.isPackaged} defaultApp=${process.defaultApp} ` +
        `resources=${process.resourcesPath}`
    );
    const resolved = resolveDataDir({
      env: process.env,
      configPath: path.join(app.getPath('userData'), CONFIG_FILENAME),
      defaultDataDir: path.join(localAppDataDir(), 'PersonalDigitalSpaceOSS', 'data'),
      installRoot: app.isPackaged ? path.dirname(app.getPath('exe')) : null,
    });
    state.dataDir = resolved.dataDir;
    log(`数据目录：${resolved.dataDir}（来源 ${resolved.source}）`);

    const launch = backendLaunch();
    const service = new BackendService({
      dataDir: resolved.dataDir,
      executable: launch.executable,
      scriptArgs: launch.scriptArgs,
      cwd: app.getPath('userData'),
      logFile: logFile(),
      onExit: (info) => {
        if (state.shuttingDown) return;
        presentError(
          new BackendError(
            '本地服务已停止',
            `后台服务意外退出（退出码 ${info.code ?? '无'}）。`,
            { detail: info.detail, exitCode: info.code }
          ),
          'runtime'
        );
      },
    });
    state.backend = service;

    const info = await service.start();
    await createMainWindow(info);
  } catch (error) {
    presentError(error, 'startup');
  }
}

// ------------------------------------------------------------- app lifecycle

app.setAppUserModelId(APP_ID);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = state.mainWindow;
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    } else if (state.errorWindow && !state.errorWindow.isDestroyed()) {
      state.errorWindow.show();
      state.errorWindow.focus();
    }
  });

  // Navigation policy for every window the app ever creates.
  app.on('web-contents-created', (event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      if (isAppUrl(url)) {
        return {
          action: 'allow',
          overrideBrowserWindowOptions: {
            width: 1120,
            height: 800,
            minWidth: 900,
            minHeight: 640,
            backgroundColor: BACKGROUND,
            autoHideMenuBar: true,
            webPreferences: {
              contextIsolation: true,
              nodeIntegration: false,
              sandbox: true,
            },
          },
        };
      }
      if (isAppUrl(contents.getURL()) && /^https:\/\/[a-z0-9-]+\.pages\.dev\/?$/.test(url)) {
        shell.openExternal(url).catch(() => log('Could not open the public website.'));
      }
      return { action: 'deny' };
    });
    contents.on('will-navigate', (event, url) => {
      if (!isAppUrl(url)) event.preventDefault();
    });
    contents.on('will-attach-webview', (event) => event.preventDefault());
  });

  app.whenReady().then(start);

  app.on('window-all-closed', () => {
    app.quit();
  });

  app.on('before-quit', (event) => {
    if (state.quitting) return;
    if (!state.backend || !state.backend.isRunning()) return;
    event.preventDefault();
    state.quitting = true;
    state.shuttingDown = true;
    state.backend
      .stop()
      .catch((error) => log(`停止后端时出错：${error && error.message}`))
      .finally(() => app.quit());
  });

  // Last resort: if the main process is exiting anyway, never leave the child.
  app.on('will-quit', () => {
    if (state.backend && state.backend.isRunning()) {
      state.backend.killNow();
    }
  });

  process.on('uncaughtException', (error) => {
    log(`主进程未捕获异常：${error && error.stack ? error.stack : error}`);
    if (state.backend) state.backend.killNow();
    app.exit(1);
  });
  process.on('unhandledRejection', (reason) => {
    log(`未处理的 Promise 拒绝：${reason && reason.stack ? reason.stack : reason}`);
  });
}
