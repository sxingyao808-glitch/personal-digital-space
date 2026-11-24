"""Desktop entry point contract: handshake, controlled stop, data-dir guard.

Every test runs against a throwaway temporary data directory and a real
``run_app.py`` child process on an OS-assigned loopback port. No real archive
data, no browser and no Electron are involved.
"""

from __future__ import annotations

import json
import http.cookiejar
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_app
from archive.db import connect, init_db
from archive.instance_lock import DataDirLock

READY_PREFIX = run_app.DESKTOP_READY_PREFIX


class BackendProcess:
    """Child backend with a line-oriented stdout reader."""

    def __init__(self, arguments: list[str], cwd: Path, stdin_pipe: bool = False) -> None:
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "run_app.py"), *arguments],
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_pipe else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._lines: queue.Queue[tuple[str, str]] = queue.Queue()
        threading.Thread(target=self._pump, args=("out",), daemon=True).start()
        threading.Thread(target=self._pump, args=("err",), daemon=True).start()

    def _pump(self, stream: str) -> None:
        handle = self.process.stdout if stream == "out" else self.process.stderr
        assert handle is not None
        for line in handle:
            if stream == "out":
                self.stdout_lines.append(line)
            else:
                self.stderr_lines.append(line)
            self._lines.put((stream, line))

    def wait_for(self, prefix: str, timeout: float = 40.0) -> str:
        deadline = time.monotonic() + timeout
        for line in list(self.stdout_lines):
            if line.startswith(prefix):
                return line
        while time.monotonic() < deadline:
            try:
                _, line = self._lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line.startswith(prefix):
                return line
        raise AssertionError(
            "未在 {:.1f}s 内看到 {}；stderr: {}".format(
                timeout, prefix, "".join(self.stderr_lines)[-2000:]
            )
        )

    def handshake(self, timeout: float = 40.0) -> dict:
        line = self.wait_for(READY_PREFIX, timeout)
        return json.loads(line[len(READY_PREFIX) :])

    def wait(self, timeout: float = 20.0) -> int:
        return self.process.wait(timeout=timeout)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for handle in (self.process.stdout, self.process.stderr):
            if handle is not None and not handle.closed:
                handle.close()


def _request(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def _post(url: str, headers: dict[str, str], timeout: float = 10.0) -> int:
    request = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        try:
            return exc.code
        finally:
            exc.close()


def _session_opener():
    """An opener that keeps the owner session cookie between requests."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


class DesktopBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="archive-desktop-")
        self.data_dir = Path(self.tempdir.name) / "data"
        self.started: list[BackendProcess] = []

    def tearDown(self) -> None:
        for process in self.started:
            process.stop()
        self.tempdir.cleanup()

    def _start(self, *, stdin_pipe: bool = False, **extra: object) -> BackendProcess:
        arguments = [
            "--desktop",
            "--data-dir",
            str(self.data_dir),
            "--port",
            "0",
            "--bootstrap-token",
            "test-bootstrap-token",
        ]
        for name, value in extra.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                arguments.append(flag)
            elif value is not None:
                arguments.extend([flag, str(value)])
        process = BackendProcess(arguments, cwd=ROOT, stdin_pipe=stdin_pipe)
        self.started.append(process)
        return process

    # ---------------------------------------------------------------- handshake
    def test_handshake_reports_live_loopback_port_without_token(self):
        process = self._start(control_token="control-secret")
        payload = process.handshake()
        self.assertEqual(payload["data_dir"], str(self.data_dir.resolve()))
        self.assertGreater(payload["port"], 1024)
        self.assertEqual(payload["schema_version"], 2)
        package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        lockfile = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["version"], lockfile["version"])
        self.assertEqual(package["version"], lockfile["packages"][""]["version"])
        self.assertEqual(payload["version"], package["version"])
        self.assertIn("pid", payload)

        status, body = _request(f"http://127.0.0.1:{payload['port']}/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"], {"status": "ok", "version": package["version"]})
        login_status, login = _request(f"http://127.0.0.1:{payload['port']}/login")
        self.assertEqual(login_status, 200)
        self.assertIn(f"版本 {package['version']}。", login.decode("utf-8"))

        # The token must never appear on stdout: the shell already owns it.
        self.assertNotIn("test-bootstrap-token", "".join(process.stdout_lines))
        self.assertNotIn("test-bootstrap-token", "".join(process.stderr_lines))

    def test_login_url_grants_owner_session(self):
        process = self._start(control_token="control-secret")
        payload = process.handshake()
        port = payload["port"]
        opener = _session_opener()
        with opener.open(
            f"http://127.0.0.1:{port}/auth?token=test-bootstrap-token", timeout=10
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Archive".encode("utf-8"), response.read())
        status, body = _request(f"http://127.0.0.1:{port}/api/session")
        self.assertEqual(status, 401, "无会话的直连请求必须被拒绝")

        with opener.open(f"http://127.0.0.1:{port}/api/session", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["data"]["owner"])
        with opener.open(f"http://127.0.0.1:{port}/api/content", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["data"]["items"], [])

    def test_static_assets_are_served(self):
        process = self._start(control_token="control-secret")
        port = process.handshake()["port"]
        for path in ("/static/app.js", "/static/styles.css"):
            status, body = _request(f"http://127.0.0.1:{port}{path}")
            self.assertEqual(status, 200, path)
            self.assertTrue(body)

    # ------------------------------------------------------------ controlled stop
    def test_shutdown_requires_the_control_token(self):
        process = self._start(control_token="control-secret")
        port = process.handshake()["port"]
        url = f"http://127.0.0.1:{port}/api/desktop/shutdown"

        self.assertEqual(_post(url, {"X-Desktop-Control": "wrong"}), 403)
        self.assertEqual(_post(url, {}), 403)
        self.assertIsNone(process.process.poll(), "错误令牌不得停止服务")
        self.assertEqual(_request(f"http://127.0.0.1:{port}/api/health")[0], 200)

        self.assertEqual(_post(url, {"X-Desktop-Control": "control-secret"}), 200)
        self.assertEqual(process.wait(timeout=15), 0)
        # The data-directory lock must be free again after a graceful stop.
        lock = DataDirLock(self.data_dir).acquire()
        lock.release()

    def test_shutdown_route_absent_without_control_token(self):
        process = self._start()
        port = process.handshake()["port"]
        status = _post(
            f"http://127.0.0.1:{port}/api/desktop/shutdown",
            {"X-Desktop-Control": "anything"},
        )
        self.assertEqual(status, 404)
        self.assertIsNone(process.process.poll())

    def test_parent_pipe_eof_stops_service(self):
        """A vanished shell must not leave a backend holding the data lock."""
        process = self._start(
            control_token="control-secret", watch_parent=True, stdin_pipe=True
        )
        payload = process.handshake()
        self.assertEqual(
            _request(f"http://127.0.0.1:{payload['port']}/api/health")[0], 200
        )
        process.process.stdin.close()
        self.assertEqual(process.wait(timeout=20), 0)
        lock = DataDirLock(self.data_dir).acquire()
        lock.release()

    def test_watch_parent_requires_desktop_mode(self):
        with self.assertRaises(SystemExit):
            run_app.main(
                ["--data-dir", str(self.data_dir), "--watch-parent", "--no-open"]
            )

    # ------------------------------------------------------------- data-dir guard
    def test_refuses_non_empty_directory_without_database(self):
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "some-user-file.txt").write_text("not an archive", encoding="utf-8")
        process = self._start()
        self.assertEqual(process.wait(timeout=30), 4)
        self.assertFalse((self.data_dir / "archive.db").exists())
        self.assertTrue((self.data_dir / "some-user-file.txt").exists())
        self.assertIn("archive.db", "".join(process.stderr_lines))

    def test_accepts_directory_holding_only_the_lock_anchor(self):
        self.data_dir.mkdir(parents=True)
        (self.data_dir / ".app.lock").write_bytes(b"")
        process = self._start(control_token="control-secret")
        payload = process.handshake()
        self.assertTrue((self.data_dir / "archive.db").exists())
        self.assertEqual(
            _post(
                f"http://127.0.0.1:{payload['port']}/api/desktop/shutdown",
                {"X-Desktop-Control": "control-secret"},
            ),
            200,
        )
        self.assertEqual(process.wait(timeout=15), 0)

    def test_empty_new_directory_is_initialised(self):
        process = self._start(control_token="control-secret")
        payload = process.handshake()
        self.assertTrue((self.data_dir / "archive.db").exists())
        self.assertEqual(
            _request(f"http://127.0.0.1:{payload['port']}/api/health")[0], 200
        )

    def test_second_instance_on_same_data_dir_exits_with_lock_code(self):
        first = self._start(control_token="control-secret")
        payload = first.handshake()
        second = self._start()
        self.assertEqual(second.wait(timeout=30), 3)
        self.assertIn("占用进程 PID", "".join(second.stderr_lines))
        self.assertEqual(
            _request(f"http://127.0.0.1:{payload['port']}/api/health")[0], 200
        )

    def test_newer_schema_is_refused(self):
        self.data_dir.mkdir(parents=True)
        init_db(self.data_dir / "archive.db")
        connection = connect(self.data_dir / "archive.db")
        try:
            connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
            connection.commit()
        finally:
            connection.close()
        process = self._start()
        self.assertEqual(process.wait(timeout=30), 2)
        self.assertIn("数据库 schema 不兼容", "".join(process.stderr_lines))

    # ------------------------------------------------------------- CLI contract
    def test_control_token_requires_desktop_mode(self):
        with self.assertRaises(SystemExit):
            run_app.main(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "--control-token",
                    "x",
                    "--no-open",
                ]
            )

    def test_desktop_mode_requires_bootstrap_token(self):
        with self.assertRaises(SystemExit):
            run_app.main(["--desktop", "--data-dir", str(self.data_dir)])

    def test_unexpected_data_dir_reason_is_side_effect_free(self):
        self.data_dir.mkdir(parents=True)
        self.assertEqual(run_app.unexpected_data_dir_reason(self.data_dir), None)
        (self.data_dir / "x.txt").write_text("x", encoding="utf-8")
        self.assertIsNotNone(run_app.unexpected_data_dir_reason(self.data_dir))
        init_db(self.data_dir / "archive.db")
        self.assertIsNone(run_app.unexpected_data_dir_reason(self.data_dir))


class DesktopShutdownRegressionTest(unittest.TestCase):
    """A graceful stop must leave a fully usable database behind."""

    def test_database_survives_graceful_stop(self):
        with tempfile.TemporaryDirectory(prefix="archive-desktop-db-") as tempdir:
            data_dir = Path(tempdir) / "data"
            process = BackendProcess(
                [
                    "--desktop",
                    "--data-dir",
                    str(data_dir),
                    "--port",
                    "0",
                    "--bootstrap-token",
                    "tok",
                    "--control-token",
                    "ctl",
                ],
                cwd=ROOT,
            )
            try:
                payload = process.handshake()
                port = payload["port"]
                self.assertEqual(
                    _post(
                        f"http://127.0.0.1:{port}/api/desktop/shutdown",
                        {"X-Desktop-Control": "ctl"},
                    ),
                    200,
                )
                self.assertEqual(process.wait(timeout=15), 0)
            finally:
                process.stop()

            connection = sqlite3.connect(str(data_dir / "archive.db"))
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("content", tables)
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "2",
                )
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
