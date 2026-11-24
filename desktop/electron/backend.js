'use strict';

/**
 * Lifecycle owner for the Python sidecar.
 *
 * Responsibilities, and nothing else:
 *  - spawn exactly one backend child for this window session,
 *  - wait for the machine-readable readiness line and a real health probe,
 *  - stop only the child it started (graceful request first, then its own PID).
 */

const { spawn, spawnSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const READY_PREFIX = 'ARCHIVE_DESKTOP_READY ';
const START_TIMEOUT_MS = 60000;
const HEALTH_TIMEOUT_MS = 12000;
const STOP_GRACE_MS = 5000;
const STDERR_TAIL_LINES = 40;

const EXIT_REASONS = {
  2: {
    title: '数据库版本不兼容',
    message: '数据文件的数据库版本与当前程序不一致，程序已停止启动。',
  },
  3: {
    title: '数据目录已被占用',
    message: '另一个「个人数字空间」实例正在使用这个数据目录。请先退出已打开的窗口。',
  },
  4: {
    title: '数据目录不可用',
    message: '所选目录不是有效的档案数据目录，程序没有在其中新建任何文件。',
  },
};

class BackendError extends Error {
  constructor(title, message, options = {}) {
    super(message);
    this.name = 'BackendError';
    this.title = title;
    this.detail = options.detail || '';
    this.exitCode = options.exitCode ?? null;
  }
}

function randomToken() {
  // Hex only: a base64url token can start with "-", which argparse would read
  // as another option and turn into an intermittent startup failure.
  return crypto.randomBytes(32).toString('hex');
}

function probeHealth(port, timeoutMs = 4000) {
  return new Promise((resolve, reject) => {
    const request = http.get(
      {
        host: '127.0.0.1',
        port,
        path: '/api/health',
        timeout: timeoutMs,
        headers: { Connection: 'close' },
      },
      (response) => {
        let body = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => {
          body += chunk;
        });
        response.on('end', () => {
          if (response.statusCode !== 200) {
            reject(new Error(`健康检查返回 HTTP ${response.statusCode}`));
            return;
          }
          try {
            const parsed = JSON.parse(body);
            if (parsed && parsed.ok && parsed.data && parsed.data.status === 'ok') {
              resolve(parsed.data);
              return;
            }
            reject(new Error('健康检查响应内容异常'));
          } catch (error) {
            reject(new Error('健康检查响应无法解析'));
          }
        });
      }
    );
    request.on('timeout', () => request.destroy(new Error('健康检查超时')));
    request.on('error', reject);
  });
}

function requestStop(port, controlToken, timeoutMs = 4000) {
  return new Promise((resolve) => {
    const payload = Buffer.from('{}', 'utf8');
    const request = http.request(
      {
        host: '127.0.0.1',
        port,
        path: '/api/desktop/shutdown',
        method: 'POST',
        timeout: timeoutMs,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': payload.length,
          'X-Desktop-Control': controlToken,
          Connection: 'close',
        },
      },
      (response) => {
        response.resume();
        response.on('end', () => resolve(response.statusCode === 200));
      }
    );
    request.on('timeout', () => request.destroy(new Error('停止请求超时')));
    request.on('error', () => resolve(false));
    request.end(payload);
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class BackendService {
  /**
   * @param {object} options
   * @param {string} options.dataDir absolute data directory
   * @param {string} options.executable backend executable or interpreter
   * @param {string[]} options.scriptArgs arguments placed before the desktop flags
   * @param {string} options.cwd existing working directory
   * @param {string} options.logFile
   * @param {(info: object) => void} [options.onExit]
   */
  constructor(options) {
    this.dataDir = options.dataDir;
    this.executable = options.executable;
    this.scriptArgs = options.scriptArgs || [];
    this.cwd = options.cwd;
    this.logFile = options.logFile;
    this.onExit = options.onExit || (() => {});

    this.bootstrapToken = randomToken();
    this.controlToken = randomToken();
    this.child = null;
    this.port = null;
    this.stopped = false;
    this.exited = false;
    this.failed = false;
    this.stderrTail = [];
    this.pending = null;
    this.ready = null;
  }

  isRunning() {
    return Boolean(this.child) && this.child.exitCode === null && !this.exited;
  }

  loginUrl() {
    return `http://127.0.0.1:${this.port}/auth?token=${this.bootstrapToken}`;
  }

  origin() {
    return `http://127.0.0.1:${this.port}`;
  }

  start() {
    if (this.ready) {
      return this.ready;
    }
    this.ready = this._start().catch((error) => {
      this.ready = null;
      throw error;
    });
    return this.ready;
  }

  async _start() {
    fs.mkdirSync(path.dirname(this.logFile), { recursive: true });

    const args = [
      ...this.scriptArgs,
      '--desktop',
      '--data-dir',
      this.dataDir,
      '--port',
      '0',
      // "=" form keeps the value attached even if it ever starts with "-".
      `--bootstrap-token=${this.bootstrapToken}`,
      `--control-token=${this.controlToken}`,
      // The backend stops itself if this process dies and the stdin pipe closes,
      // so a crashed shell can never leave a backend holding the data lock.
      '--watch-parent',
    ];

    // Never log argv: it carries the tokens.
    this._log(`启动后端：executable=${this.executable} data_dir=${this.dataDir}`);

    this.child = spawn(this.executable, args, {
      cwd: this.cwd,
      windowsHide: true,
      // stdin stays an open pipe on purpose: it is the parent-death channel.
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
      },
    });

    const readyPromise = new Promise((resolve, reject) => {
      this.pending = { resolve, reject };
    });

    let stdoutBuffer = '';
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk;
      let index = stdoutBuffer.indexOf('\n');
      while (index >= 0) {
        const line = stdoutBuffer.slice(0, index).replace(/\r$/, '');
        stdoutBuffer = stdoutBuffer.slice(index + 1);
        this._handleStdoutLine(line);
        index = stdoutBuffer.indexOf('\n');
      }
    });
    if (this.child.stderr) {
      this.child.stderr.setEncoding('utf8');
      this.child.stderr.on('data', (chunk) => {
        chunk
          .split(/\r?\n/)
          .filter((line) => line.trim() !== '')
          .forEach((line) => this._recordLine(line));
      });
    }

    this.child.once('error', (error) => {
      this.exited = true;
      const code = error && error.code ? error.code : '';
      const title = code === 'ENOENT' ? '找不到后端程序' : '后端启动失败';
      const message =
        code === 'ENOENT'
          ? '桌面应用无法定位内置的后端程序，安装可能不完整。'
          : '后端进程无法启动。';
      this._fail(new BackendError(title, message, { detail: String(error && error.message) }));
    });

    this.child.once('exit', (code, signal) => {
      this.exited = true;
      this._log(`后端退出：code=${code} signal=${signal}`);
      if (this.stopped || this.failed) {
        return;
      }
      if (this.pending) {
        const reason = EXIT_REASONS[code];
        const detail = this._stderrText();
        const usageError = code === 2 && /usage:|error: argument/i.test(detail);
        this._fail(
          new BackendError(
            usageError ? '本地服务启动参数被拒绝' : reason ? reason.title : '后端启动失败',
            usageError
              ? '后端拒绝了启动参数，程序无法继续。'
              : reason
                ? reason.message
                : `后端进程在就绪前退出（退出码 ${code ?? '无'}）。`,
            { exitCode: code, detail }
          )
        );
        return;
      }
      this.onExit({ code, signal, detail: this._stderrText(), port: this.port });
    });

    const timeout = setTimeout(() => {
      this._fail(
        new BackendError('后端启动超时', '本地服务在限定时间内没有就绪。', {
          detail: this._stderrText(),
        })
      );
      this._killTree();
    }, START_TIMEOUT_MS);

    let handshake;
    try {
      handshake = await readyPromise;
    } finally {
      clearTimeout(timeout);
    }

    this.port = handshake.port;
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    let lastError = null;
    while (Date.now() < deadline) {
      try {
        await probeHealth(this.port);
        lastError = null;
        break;
      } catch (error) {
        lastError = error;
        await delay(200);
      }
    }
    if (lastError) {
      this.failed = true;
      this._killTree();
      throw new BackendError('本地服务未通过健康检查', '服务已启动但无法响应健康检查。', {
        detail: this._stderrText() || String(lastError.message),
      });
    }

    this._log(`后端就绪：port=${this.port} data_dir=${handshake.data_dir}`);
    return {
      port: this.port,
      pid: handshake.pid,
      dataDir: handshake.data_dir,
      version: handshake.version,
      schemaVersion: handshake.schema_version,
      loginUrl: this.loginUrl(),
    };
  }

  _handleStdoutLine(line) {
    if (line.startsWith(READY_PREFIX)) {
      if (!this.pending) {
        return;
      }
      try {
        const payload = JSON.parse(line.slice(READY_PREFIX.length));
        const resolve = this.pending.resolve;
        this.pending = null;
        resolve(payload);
      } catch (error) {
        this._fail(
          new BackendError('后端握手失败', '后端返回的就绪信息无法解析。', {
            detail: String(error && error.message),
          })
        );
      }
      return;
    }
    if (line.trim() !== '') {
      this._recordLine(line);
    }
  }

  _recordLine(line) {
    this.stderrTail.push(line);
    if (this.stderrTail.length > STDERR_TAIL_LINES) {
      this.stderrTail.shift();
    }
    this._log(`backend: ${line}`);
  }

  _stderrText() {
    return this.stderrTail.join('\n');
  }

  _fail(error) {
    this.failed = true;
    if (!this.pending) {
      return;
    }
    const reject = this.pending.reject;
    this.pending = null;
    reject(error);
  }

  _log(line) {
    try {
      fs.appendFileSync(
        this.logFile,
        `[${new Date().toISOString()}] ${line}\n`,
        'utf8'
      );
    } catch (error) {
      // Logging must never break the app.
    }
  }

  _killTree() {
    const child = this.child;
    if (!child || child.exitCode !== null) {
      return;
    }
    if (process.platform === 'win32' && child.pid) {
      // /T limits the kill to this exact process tree; no image-name matching.
      // It runs before child.kill() so the parent still owns the tree when the
      // tree walk happens.
      spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
        windowsHide: true,
        stdio: 'ignore',
      });
    }
    try {
      child.kill();
    } catch (error) {
      // Already gone.
    }
  }

  /** Synchronous last resort used when the shell itself is going down. */
  killNow() {
    this.stopped = true;
    this._killTree();
  }

  async stop() {
    if (this.stopped) {
      return;
    }
    this.stopped = true;
    if (!this.isRunning()) {
      this._killTree();
      return;
    }
    this._log('请求后端优雅停止');
    if (this.port) {
      await requestStop(this.port, this.controlToken);
    }
    const deadline = Date.now() + STOP_GRACE_MS;
    while (Date.now() < deadline && this.isRunning()) {
      await delay(100);
    }
    if (this.isRunning()) {
      this._log('优雅停止超时，终止本次启动的子进程树');
      this._killTree();
      const hardDeadline = Date.now() + 3000;
      while (Date.now() < hardDeadline && this.isRunning()) {
        await delay(100);
      }
    }
    this._log('后端已停止');
  }
}

module.exports = { BackendService, BackendError, EXIT_REASONS, probeHealth };
