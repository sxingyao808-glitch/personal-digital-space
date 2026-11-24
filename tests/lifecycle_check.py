"""Real-process lifecycle check on the current operating system.

Starts the actual launcher twice against the same data directory, proves the
owner session boundary, creates content through the HTTP API, restarts the
process, and reads the same stable identity back. Output is JSON for evidence.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "lifecycle-owner-token-0123456789"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.raw_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )

    def request(self, method: str, url: str, *, body=None, headers=None, follow=True):
        data = None
        request_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        opener = self.opener if follow else self.raw_opener
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, url: str, **kwargs):
        status, payload = self.request(method, url, **kwargs)
        try:
            return status, json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return status, None


def wait_for_health(base_url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def start_server(data_dir: Path, port: int, init_fixture: bool) -> subprocess.Popen:
    command = [
        sys.executable,
        str(ROOT / "run_app.py"),
        "--data-dir",
        str(data_dir),
        "--port",
        str(port),
        "--no-open",
        "--bootstrap-token",
        TOKEN,
    ]
    if init_fixture:
        command.append("--init-fixture")
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def check() -> dict:
    result = {"ok": False, "steps": [], "errors": []}
    with tempfile.TemporaryDirectory() as folder:
        data_dir = Path(folder) / "data"
        port = free_port()
        base = f"http://127.0.0.1:{port}"

        process = start_server(data_dir, port, init_fixture=True)
        try:
            if not wait_for_health(base):
                result["errors"].append("server did not become healthy")
                return result
            result["steps"].append("server_started")

            anonymous = Client()
            status, _ = anonymous.request("GET", f"{base}/", follow=False)
            result["steps"].append({"unauthenticated_index_status": status})
            if status != 302:
                result["errors"].append(f"expected unauthenticated redirect, got {status}")

            status, _ = anonymous.json("GET", f"{base}/api/content")
            result["steps"].append({"unauthenticated_api_status": status})
            if status != 401:
                result["errors"].append(f"expected unauthenticated API 401, got {status}")

            owner = Client()
            status, _ = owner.request("GET", f"{base}/auth?token={TOKEN}")
            result["steps"].append({"login_status": status})
            if status != 200:
                result["errors"].append(f"expected login to reach index, got {status}")

            status, session = owner.json("GET", f"{base}/api/session")
            if status != 200 or not session or not session.get("ok"):
                result["errors"].append("owner session not established")
                return result
            csrf = session["data"]["csrf"]
            result["steps"].append(
                {
                    "session_established": True,
                    "data_dir": session["data"]["data_dir"],
                }
            )

            status, created = owner.json(
                "POST",
                f"{base}/api/content",
                body={"title": "生命周期记录", "body": "重启后应保留", "visibility": None},
                headers={"X-CSRF-Token": csrf},
            )
            if status != 201 or not created or not created.get("ok"):
                result["errors"].append(f"content creation failed: {status} {created}")
                return result
            content = created["data"]
            result["steps"].append(
                {"created_id": content["id"], "revision": content["revision"]}
            )

            status, _ = owner.json(
                "POST",
                f"{base}/api/content",
                body={"title": "无 CSRF 请求"},
            )
            result["steps"].append({"csrf_missing_status": status})
            if status != 403:
                result["errors"].append(f"expected missing-CSRF 403, got {status}")
        finally:
            stop_server(process)

        time.sleep(0.5)
        port2 = free_port()
        base2 = f"http://127.0.0.1:{port2}"
        process2 = start_server(data_dir, port2, init_fixture=False)
        try:
            if not wait_for_health(base2):
                result["errors"].append("restarted server did not become healthy")
                return result
            result["steps"].append("server_restarted")

            owner2 = Client()
            status, _ = owner2.request("GET", f"{base2}/auth?token={TOKEN}")
            if status != 200:
                result["errors"].append(f"relogin failed: {status}")
            status, session2 = owner2.json("GET", f"{base2}/api/session")
            if status != 200 or not session2 or not session2.get("ok"):
                result["errors"].append("owner session after restart not established")
                return result

            status, reread = owner2.json(
                "GET",
                f"{base2}/api/content/{content['id']}",
                headers={"X-CSRF-Token": session2["data"]["csrf"]},
            )
            if status != 200 or not reread or not reread.get("ok"):
                result["errors"].append(f"identity not readable after restart: {status}")
            else:
                item = reread["data"]
                result["steps"].append(
                    {
                        "id_after_restart": item["id"],
                        "title": item["title"],
                        "body": item["body"],
                        "same_identity": item["id"] == content["id"],
                    }
                )
                if item["id"] != content["id"] or item["body"] != "重启后应保留":
                    result["errors"].append("content identity or body changed across restart")

            status, _ = owner2.json("GET", f"{base2}/api/content", headers={"X-CSRF-Token": ""})
            result["steps"].append({"authenticated_get_without_csrf": status})
            if status != 200:
                result["errors"].append(f"owner GET should not require CSRF header, got {status}")
        finally:
            stop_server(process2)

    result["ok"] = not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    report = check()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
