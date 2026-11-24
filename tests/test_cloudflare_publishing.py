"""End-to-end publishing against synthetic Cloudflare HTTP responses."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4
from zipfile import ZipFile

from archive import __version__
from archive.app import CSP, create_app
from archive.backup import ArchiveBackup
from archive.cloudflare_pages import PagesClient, PagesError, WindowsCredentials
from archive.release import public_content_id
from archive.pages_hosting import PagesHosting, public_path

ACCOUNT = "c" * 32
PROJECT = "synthetic-space"
PROJECT_ID = "11111111-2222-4333-8444-555555555555"
OWNER = "synthetic-owner-test-54321"
TOKEN = "synthetic-not-a-real-cloudflare-token"
DOMAIN = PROJECT + ".pages.dev"


class MemoryCredentials:
    def __init__(self):
        self.entries = {}

    def save(self, key, token):
        self.entries[key] = token

    def read(self, key):
        return self.entries[key]

    def delete(self, key):
        self.entries.pop(key, None)


class PagesSimulator:
    def __init__(self):
        self.credentials = MemoryCredentials()
        self.assets = {}
        self.deployments = {}
        self.files = {}
        self.canonical = None
        self.uploads = 0
        self.creates = 0
        self.failure = ""
        self.headers = b""
        self.deleted = []
        self.delete_attempts = []
        self.public_overrides = {}
        self.cache_result = "UNSUPPORTED"
        self.pops = {"local": "TPE", "independent": "LAX"}

    def install(self, app):
        hosting = app.extensions["pages_hosting"]
        hosting.credentials = self.credentials
        hosting.client_factory = self.client
        hosting.poll_delay = 0
        hosting.probe_proxy = "http://synthetic-probe:8080"
        hosting.cache_observer = self.cache_observation

    def cache_observation(self, host, deployment_id, paths):
        assert host == DOMAIN and deployment_id == self.canonical and paths
        if isinstance(self.cache_result, BaseException):
            raise self.cache_result
        return self.cache_result

    def client(self, account, name, token):
        client = PagesClient(account, name, token)
        client._request = self.request
        client.direct_public_probe = lambda: client.public
        client.public_probe = lambda proxy: (
            lambda host, path, **kwargs: self.public(kwargs["method"], f"https://{host}/" + quote(path, safe="/"),
                                                    outlet="independent")
        )
        return client

    @staticmethod
    def success(value):
        return 200, {}, json.dumps({"success": True, "result": value}).encode()

    def request(self, method, url, *, data=None, headers=None, limit=0, error_limit=0, allow_truncated=False,
                keep_error_prefix=False):
        if self.failure == "offline":
            raise PagesError("网络连接中断或超时。")
        if "api.cloudflare.com" not in url:
            assert not headers.get("Authorization")
            return self.public(method, url)
        path = url.split("/client/v4", 1)[1]
        base = f"/accounts/{ACCOUNT}/pages/projects/{PROJECT}"
        if headers["Authorization"] not in {"Bearer " + TOKEN, "Bearer synthetic.jwt"}:
            return 403, {}, b""
        if path == f"/accounts/{ACCOUNT}/pages/projects?per_page=10&page=1":
            return self.success([{"name": PROJECT}, {"name": "git-project", "source": {"type": "github"}}])
        if self.failure == "missing_project" and path == base:
            return 404, {}, b""
        if method == "GET" and path == base:
            return self.success({
                "id": PROJECT_ID, "name": PROJECT, "production_branch": "main",
                "subdomain": DOMAIN, "source": None,
                "canonical_deployment": {"id": self.canonical} if self.canonical else None,
            })
        if path == base + "/upload-token":
            return self.success({"jwt": "synthetic.jwt"})
        if path == "/pages/assets/check-missing":
            return self.success([key for key in json.loads(data)["hashes"] if key not in self.assets])
        if path == "/pages/assets/upload":
            self.uploads += 1
            if self.failure == "upload":
                raise PagesError("上传连接中断。")
            for row in json.loads(data):
                assert row["base64"]
                self.assets[row["key"]] = (base64.b64decode(row["value"], validate=True), row["metadata"]["contentType"])
            return self.success([])
        if path == "/pages/assets/upsert-hashes":
            return self.success([])
        if method == "POST" and path == base + "/deployments":
            self.creates += 1
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + data
            )
            parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                     for part in message.iter_parts()}
            assert parts["branch"] == b"main"
            self.headers = parts["_headers"]
            files = {path: self.assets[key] for path, key in json.loads(parts["manifest"]).items()}
            deployment_id = str(uuid4())
            self.deployments[deployment_id] = {
                "id": deployment_id, "environment": "production",
                "project_id": PROJECT_ID, "url": f"https://{deployment_id[:8]}.{DOMAIN}",
                "created_on": "2026-11-21T14:14:02Z",
                "latest_stage": {"name": "deploy", "status": "success"},
                "deployment_trigger": {"metadata": {"commit_message": parts["commit_message"].decode()}},
            }
            self.files[deployment_id] = files
            self.canonical = deployment_id
            if self.failure == "create_response":
                raise PagesError("提交后网络中断。")
            return self.success({"id": deployment_id})
        if path.startswith(base + "/deployments?"):
            return self.success(list(self.deployments.values()))
        if path.startswith(base + "/deployments/"):
            deployment_id = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.delete_attempts.append(deployment_id)
                if self.failure == "cleanup":
                    return 502, {}, b""
                assert deployment_id != self.canonical
                self.deleted.append(deployment_id)
                self.deployments.pop(deployment_id, None)
                if self.failure == "cleanup_reply":
                    raise PagesError("部署移除回执丢失。", timeout=True)
                return self.success(None)
            if self.failure == "poll":
                return 502, {}, b""
            if deployment_id not in self.deployments:
                return 404, {}, b""
            return self.success(self.deployments[deployment_id])
        raise AssertionError((method, path))

    def public(self, method, url, *, outlet="local"):
        request = urlsplit(url)
        requested = unquote(request.path.lstrip("/"))
        host = request.hostname
        ray = "0123456789abcdef-" + self.pops[outlet]
        override = self.public_overrides.get((outlet, host, requested, method))
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            code, headers, body = override
            return code, {"CF-Ray": ray, **headers}, body
        deployment = self.canonical if host == DOMAIN else next(
            (row["id"] for row in self.deployments.values() if urlsplit(row["url"]).hostname == host), None,
        )
        files = self.files.get(deployment, {})
        for path, (body, mime) in files.items():
            if public_path(path[1:]) == requested:
                csp = "sandbox; default-src 'none'" if path.startswith("/attachments/") else CSP
                headers = {"Content-Type": mime, "Content-Security-Policy": csp, "CF-Ray": ray,
                           "X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment",
                           "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                           "Cache-Control": "no-store, no-transform"}
                return 200, headers, b"" if method == "HEAD" else body
        return 404, {"CF-Ray": ray}, b""


class CloudflareDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = PagesClient(ACCOUNT, PROJECT, TOKEN)

    @staticmethod
    def page(start, count, *, total_pages=None):
        envelope = {
            "success": True,
            "result": [{"name": f"project-{number:03}"} for number in range(start, start + count)],
        }
        if total_pages is not None:
            envelope["result_info"] = {"total_pages": total_pages}
        return 200, {}, json.dumps(envelope).encode()

    def assert_requested_pages(self, transport, count):
        self.assertEqual(
            [call.args for call in transport.call_args_list],
            [("GET", f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects?per_page=10&page={page}")
             for page in range(1, count + 1)],
        )

    def test_pagination_uses_page_size_when_metadata_is_missing(self):
        for sizes in ((3,), (10, 4), (10, 0), (10, 10, 3)):
            with self.subTest(sizes=sizes):
                pages = [self.page(sum(sizes[:index]), count) for index, count in enumerate(sizes)]
                with patch.object(self.client, "_request", side_effect=pages) as transport:
                    self.assertEqual(self.client.projects(ACCOUNT),
                                     [f"project-{number:03}" for number in range(sum(sizes))])
                self.assert_requested_pages(transport, len(sizes))

    def test_total_pages_takes_priority_over_page_size(self):
        pages = [self.page(0, 2, total_pages=2), self.page(2, 10, total_pages=2)]
        with patch.object(self.client, "_request", side_effect=pages) as transport:
            self.assertEqual(self.client.projects(ACCOUNT), [f"project-{number:03}" for number in range(12)])
        self.assert_requested_pages(transport, 2)

    def test_page_limit_reports_incomplete_results(self):
        for total_pages in (None, 11):
            with self.subTest(total_pages=total_pages):
                count = 10 if total_pages is None else 3
                pages = [self.page(index * count, count, total_pages=total_pages) for index in range(10)]
                with patch.object(self.client, "_request", side_effect=pages) as transport:
                    with self.assertRaisesRegex(PagesError, "安全上限.*未能完整获取"):
                        self.client.projects(ACCOUNT)
                self.assert_requested_pages(transport, 10)

    def test_tenth_page_can_be_the_last(self):
        pages = [self.page(index * 10, 10, total_pages=10) for index in range(10)]
        with patch.object(self.client, "_request", side_effect=pages) as transport:
            self.assertEqual(len(self.client.projects(ACCOUNT)), 100)
        self.assert_requested_pages(transport, 10)

    def test_oversized_error_body_and_invalid_ray_are_omitted(self):
        headers = Message()
        headers["CF-Ray"] = "invalid-ray"
        body = io.BytesIO(b"x" * (64 * 1024))
        with patch.object(self.client.opener, "open", side_effect=HTTPError("", 400, "", headers, body)):
            with self.assertRaises(PagesError) as caught:
                self.client.projects(ACCOUNT)
        self.assertTrue(body.closed)
        self.assertEqual(caught.exception.details, {"cloudflare": {"http_status": 400, "errors": []}})


class CloudflarePublicVerificationTests(unittest.TestCase):
    def setUp(self):
        self.client = PagesClient(ACCOUNT, PROJECT, TOKEN)
        self.hosting = PagesHosting(Path("."), None, CSP)
        self.html = b"<h1>public page</h1>"
        self.headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store, no-transform",
        }
        self.record = {
            "samples": {"index.html": {
                "size": len(self.html),
                "sha256": hashlib.sha256(self.html).hexdigest(),
                "mime": "text/html",
            }},
            "paths": ["index.html"],
        }

    def verify(self):
        self.hosting._verify_public(self.client, {"domain": DOMAIN}, self.record, None)

    def test_public_get_and_head_send_application_user_agent_without_credentials(self):
        headers = Message()
        body = io.BytesIO(b"error code: 1010\n")
        errors = [HTTPError("", 403, "", headers, body),
                  HTTPError("", 404, "", headers, io.BytesIO())]
        with patch.object(self.client.opener, "open", side_effect=errors) as opener:
            self.assertEqual(self.client.public(DOMAIN, "", limit=len(self.html))[2], b"error code: 1010\n")
            self.assertEqual(self.client.public(DOMAIN, "releases")[0], 404)
        requests = [call.args[0] for call in opener.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "HEAD"])
        for request in requests:
            self.assertEqual(request.get_header("User-agent"),
                             f"PersonalDigitalSpace/{__version__} (Cloudflare Pages verification)")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_response_failures_are_classified_by_first_failing_check(self):
        ray = "a5dfbac83fdfb9a1-TPE"
        cases = [
            (403, {"CF-Ray": ray}, b"error code: 1010\n", "access_denied", "Cloudflare 拒绝访问"),
            (503, {}, b"unavailable", "http_error", "请求失败"),
            (308, {"Location": "/"}, b"", "http_error", "请求失败"),
            (404, {}, b"missing", "file_mismatch", "文件不存在"),
            (200, self.headers, self.html.replace(b"h1", b"h2"), "file_mismatch", "文件内容"),
            (200, self.headers, self.html + b"extra", "file_mismatch", "文件内容"),
            (200, {**self.headers, "Content-Security-Policy": ""}, self.html, "header_mismatch", "响应头核验失败"),
            (200, {**self.headers, "X-Content-Type-Options": "other"}, self.html, "header_mismatch", "响应头核验失败"),
            (200, {**self.headers, "Content-Type": "text/plain"}, self.html, "header_mismatch", "响应头核验失败"),
        ]
        for status, headers, body, kind, message in cases:
            with self.subTest(status=status, kind=kind, message=message):
                with patch.object(self.client, "public", return_value=(status, headers, body)):
                    with self.assertRaisesRegex(PagesError, message) as caught:
                        self.verify()
                verification = caught.exception.details["verification"]
                self.assertEqual(verification["kind"], kind)
                self.assertEqual(verification["http_status"], status)
                if status == 403:
                    self.assertEqual(verification["cloudflare_code"], 1010)
                    self.assertEqual(verification["ray_id"], ray)

    def test_complete_verification_and_expected_hidden_responses(self):
        responses = [(200, self.headers, self.html), (404, {}, b""), (410, {}, b"")]
        with patch.object(self.client, "public", side_effect=responses) as request:
            self.verify()
        self.assertEqual([call.args[1] for call in request.call_args_list], ["", "api/content", "releases"])

    def test_hidden_endpoint_denial_does_not_pass_as_missing(self):
        responses = [(200, self.headers, self.html),
                     (403, {"CF-Error-Type": "1010", "CF-Ray": "invalid" + TOKEN, "Set-Cookie": TOKEN},
                      ("Authorization: Bearer " + TOKEN).encode())]
        with patch.object(self.client, "public", side_effect=responses):
            with self.assertRaisesRegex(PagesError, "Cloudflare 拒绝访问") as caught:
                self.verify()
        self.assertEqual(caught.exception.details["verification"],
                         {"kind": "access_denied", "method": "HEAD", "http_status": 403, "cloudflare_code": 1010})
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_independent_probe_rejects_proxy_credentials_and_has_no_api_credentials(self):
        for invalid in ("http://name:secret@proxy:8080", "https://proxy:8080", "http://proxy:8080/path"):
            with self.subTest(proxy=invalid), self.assertRaises(PagesError):
                self.client.public_probe(invalid)
        with patch("archive.cloudflare_pages.build_opener") as build:
            headers = Message()
            build.return_value.open.side_effect = HTTPError("", 404, "", headers, io.BytesIO(b"not found"))
            probe = self.client.public_probe("http://localhost:8080")
            self.assertEqual(probe(DOMAIN, "attachments/a/file.txt", method="GET", limit=12),
                             (404, headers, b"not found"))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(request.full_url, f"https://{DOMAIN}/attachments/a/file.txt")

    def test_configured_independent_probe_pairs_with_explicit_direct_transport(self):
        self.hosting.probe_proxy = "http://localhost:8080"
        with patch.object(self.client, "direct_public_probe") as direct, patch.object(self.client, "public_probe") as proxy:
            self.assertEqual(self.hosting._probes(self.client), {"local": direct.return_value,
                                                                "independent": proxy.return_value})
        direct.assert_called_once_with()
        proxy.assert_called_once_with(self.hosting.probe_proxy)


class CloudflarePublishingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"
        self.remote = PagesSimulator()
        self.reopen()

    def reopen(self):
        self.app = create_app(self.data, bootstrap_token=OWNER, testing=True)
        self.remote.install(self.app)
        self.owner = self.app.test_client()
        self.owner.get("/auth?token=" + OWNER)
        with self.owner.session_transaction() as session:
            self.csrf = {"X-CSRF-Token": session["csrf"]}
        self.store = self.app.extensions["archive_store"]

    def post(self, path, value=None):
        return self.owner.post(path, json=value or {}, headers=self.csrf)

    def get(self, path):
        with self.owner.get(path) as response:
            return response.get_json()["data"]

    def configure(self, token=TOKEN):
        return self.post("/api/publish/cloudflare/configure",
                         {"account": ACCOUNT, "project": PROJECT, "token": token})

    def publish(self, **value):
        plan = self.get("/api/publish/plan")
        return self.post("/api/publish/one-click", {
            "confirm": True, "online": True, "confirmation": plan["confirmation"],
            "expected_active": plan["active_release_id"], **value,
        })

    def activate(self, **value):
        response = self.publish(**value)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def check(self):
        response = self.post("/api/publish/cloudflare/check")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]

    def approve(self, alias, **overrides):
        plan = self.get("/api/publish/plan")
        return self.post("/api/publish/cloudflare/cleanup", {
            "confirm": True, "target_id": PROJECT_ID, "deployment_id": alias["id"], "approval": alias["approval"],
            "expected_active": plan["active_release_id"], **overrides,
        })

    def stored(self):
        return json.loads((self.data / "hosting" / "pages.json").read_text(encoding="utf-8"))["records"]

    def replaced_attachment(self):
        item = self.store.create_content("合成条目", "版本一", visibility="public")

        def upload(payload, suffix):
            response = self.owner.post(f"/api/content/{item['id']}/attachments", headers=self.csrf, data={
                "file": (io.BytesIO(payload), "版本 测试.txt"),
                "operation_id": "retirement_test_" + suffix, "visibility": "public",
            })
            self.assertEqual(response.status_code, 201)
            return response.get_json()["data"]["attachment"]["id"]

        before = b"synthetic old payload"
        old_id = upload(before, "old")
        self.configure()
        self.assertEqual(self.activate()["deployment"]["status"], "succeeded")
        old = self.remote.canonical
        path = next(path[1:] for path in self.remote.files[old] if path.startswith("/attachments/"))
        response = self.owner.patch(f"/api/attachments/{old_id}", json={"visibility": "private"}, headers=self.csrf)
        self.assertEqual(response.status_code, 200)
        upload(b"synthetic new payload", "new")
        self.store.update_content(item["id"], item["revision"], title="合成条目更新", body="版本二", summary="新版摘要")
        return old, path, before

    def test_project_discovery_uses_saved_credential_after_restart(self):
        self.assertEqual(self.configure().status_code, 200)
        self.reopen()
        response = self.post("/api/publish/cloudflare/projects", {"account": ACCOUNT, "token": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["projects"], [PROJECT])

    def test_verifying_state_converges_persistently_without_deploying_again(self):
        self.store.create_content("已公开", "公开正文", visibility="public")
        self.assertEqual(self.configure().status_code, 200)
        state_file = self.data / "hosting" / "pages.json"
        ray = "a5dfbac83fdfb9a1-TPE"
        attempts = []

        def blocked(method, url):
            record = json.loads(state_file.read_text(encoding="utf-8"))["records"][-1]
            attempts.append((record["status"], bool(record.get("production_verified"))))
            return 403, {"CF-Ray": ray, "Set-Cookie": TOKEN}, b"error code: 1010\n"

        with self.assertNoLogs(self.app.logger), patch.object(self.remote, "public", side_effect=blocked):
            pending = self.activate()["deployment"]
            self.assertEqual(pending["status"], "unconfirmed")
            retry = self.post("/api/publish/cloudflare/check").get_json()["data"]
            self.assertEqual(retry["status"], "unconfirmed")
        self.assertEqual(attempts, [("verifying", False), ("verifying", False)])
        self.assertEqual(self.remote.creates, 1)
        verification = {"kind": "access_denied", "method": "GET", "http_status": 403,
                        "cloudflare_code": 1010, "ray_id": ray}
        self.assertEqual(retry["last_attempt"]["verification_error"], verification)
        self.assertNotIn(TOKEN, json.dumps(retry))
        stored = state_file.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, stored)
        self.assertNotIn("Set-Cookie", stored)
        self.reopen()
        pending = self.get("/api/publication/state")["deployment"]
        self.assertEqual(pending["status"], "unconfirmed")
        self.assertEqual(pending["last_attempt"]["verification_error"], verification)

        completed = self.post("/api/publish/cloudflare/check").get_json()["data"]
        self.assertEqual(completed["status"], "succeeded")
        self.assertIsNone(completed["last_attempt"]["verification_error"])
        persisted = json.loads(state_file.read_text(encoding="utf-8"))["records"][-1]
        self.assertEqual(persisted["status"], "succeeded")
        self.assertTrue(persisted["production_verified"])
        self.assertNotIn("verification_error", persisted)
        self.reopen()
        for path in ("/api/publication/state", "/api/publish/plan", "/api/publication/state"):
            restored = self.get(path)["deployment"]
            self.assertEqual(restored["status"], "succeeded")
            self.assertEqual(restored["public_url"], "https://" + DOMAIN)
        self.assertEqual(self.remote.creates, 1)
        self.assertEqual(self.remote.deleted, [])

    def test_project_discovery_returns_only_redacted_upstream_error_details(self):
        result = {
            "success": False,
            "errors": [
                {"code": 8000024, "message": "Invalid list options provided. Review the page or per_page parameter."},
                {"code": 9000, "message": f"Echoed credential {TOKEN}\nAuthorization: Basic short-secret",
                 "source": {"pointer": "sensitive-source"}},
                {"code": "not-numeric", "message": "ignored"},
            ],
            "result": "sensitive-response-body",
        }
        headers = Message()
        headers["CF-Ray"] = "a5df70267830c97a-TPE"
        headers["Set-Cookie"] = "sensitive-header"
        error = HTTPError("", 400, "", headers, io.BytesIO(json.dumps(result).encode()))
        self.app.extensions["pages_hosting"].client_factory = PagesClient
        with patch("archive.cloudflare_pages.build_opener") as opener:
            opener.return_value.open.side_effect = error
            response = self.post("/api/publish/cloudflare/projects", {"account": ACCOUNT, "token": TOKEN})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"], {
            "code": "cloudflare_pages",
            "message": "Cloudflare API 请求未成功。",
            "details": {"cloudflare": {
                "http_status": 400,
                "errors": [
                    {"code": 8000024, "message": result["errors"][0]["message"]},
                    {"code": 9000, "message": "Echoed credential [已脱敏] [已脱敏]"},
                ],
                "ray_id": "a5df70267830c97a-TPE",
            }},
        })

    def test_first_publish_update_dedup_force_and_restart(self):
        item = self.store.create_content("初稿", "测试正文", visibility="public")
        private = self.store.create_content("私密标题", "PRIVATE-CONTENT-SENTINEL", visibility="private")
        self.store.add_relation(item["id"], private["id"], "REFERENCES")
        found = self.post("/api/publish/cloudflare/projects", {"account": ACCOUNT, "token": TOKEN})
        self.assertEqual(found.get_json()["data"]["projects"], [PROJECT])
        self.assertEqual(self.configure().status_code, 200)
        self.assertEqual(self.remote.creates, 0)
        self.assertEqual(self.publish(confirm=False).status_code, 422)
        self.assertEqual(self.publish(confirmation="stale").status_code, 409)
        first = self.activate()
        self.assertEqual(first["deployment"]["status"], "succeeded", first["deployment"])
        self.assertEqual(first["deployment"]["public_url"], "https://" + DOMAIN)
        files = self.remote.files[self.remote.canonical]
        self.assertIn("/index.html", files)
        self.assertIn("/404.html", files)
        self.assertIn(b"Content-Security-Policy", self.remote.headers)
        self.assertNotIn("/_headers", files)
        self.assertNotIn("/release.json", files)
        self.assertFalse(any(path.endswith(".json") for path in files))
        self.assertTrue(all(b"PRIVATE-CONTENT-SENTINEL" not in raw and TOKEN.encode() not in raw
                            and b'name="release-id"' not in raw and "私密标题".encode() not in raw
                            for raw, _ in files.values()))
        self.assertEqual(self.activate()["release_id"], first["release_id"])
        self.assertEqual(self.remote.creates, 1)
        self.assertEqual(self.activate(force=True)["release_id"], first["release_id"])
        self.assertEqual(self.remote.creates, 2)
        rebuilt = self.post("/api/publish/one-click", {"confirm": True, "force": True}).get_json()["data"]
        self.assertNotEqual(rebuilt["release_id"], first["release_id"])
        self.assertEqual(self.activate()["release_id"], rebuilt["release_id"])
        self.assertEqual(self.remote.creates, 2)
        self.store.update_content(item["id"], item["revision"], title="更新后的文章")
        updated = self.activate()
        self.assertNotEqual(updated["release_id"], first["release_id"])
        self.reopen()
        state = self.get("/api/publication/state")["deployment"]
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["current_release_id"], updated["release_id"])
        self.assertEqual(len(state["history"]), 3)
        self.assertNotIn(TOKEN, (self.data / "hosting" / "pages.json").read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, json.dumps(state))
        backup = self.data.parent / "archive.zip"
        ArchiveBackup(self.data).create(backup)
        with ZipFile(backup) as archive:
            self.assertFalse(any("hosting/" in name for name in archive.namelist()))
            for name in archive.namelist():
                self.assertNotIn(TOKEN.encode(), archive.read(name))

    def test_last_private_item_requires_verified_site_and_old_deployment_removal(self):
        item = self.store.create_content("曾公开", "公开正文", visibility="public")
        self.configure()
        self.activate()
        old = self.remote.canonical
        self.store.update_content(item["id"], item["revision"], visibility="private")
        self.assertEqual(self.remote.canonical, old)
        self.assertEqual(self.get("/api/publication/state")["deployment"]["pending_removal_ids"], [item["id"]])
        attempt = self.activate(remove=True)
        self.assertEqual(attempt["local_publish"], "withdrawn")
        self.assertEqual(attempt["deployment"]["status"], "unconfirmed")
        self.assertIn(old, self.remote.deployments)
        self.assertEqual(self.remote.public("HEAD", f"https://{DOMAIN}/content/{public_content_id(item['id'])}")[0], 404)
        self.assertEqual(self.get("/api/publication/state")["contents"][0]["state"], "pending_removal")
        ready = self.check()
        self.assertEqual(ready["status"], "cleanup_pending")
        self.assertEqual(self.activate(remove=True)["deployment"]["status"], "cleanup_pending")
        self.assertEqual(self.remote.delete_attempts, [])
        candidate = ready["verification"]["aliases"][0]
        self.assertEqual(self.approve(candidate).status_code, 200)
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertNotIn(old, self.remote.deployments)
        self.assertEqual(self.remote.creates, 2)
        self.assertEqual(self.get("/api/publication/state")["deployment"]["pending_removal_ids"], [])

    def test_upload_failure_preserves_old_site_and_reuses_local_release(self):
        item = self.store.create_content("旧标题", "旧正文", visibility="public")
        self.configure()
        self.activate()
        old = self.remote.canonical
        self.store.update_content(item["id"], item["revision"], title="新标题")
        self.remote.failure = "upload"
        failed = self.activate()
        self.assertEqual(failed["deployment"]["status"], "failed")
        self.assertEqual(self.remote.canonical, old)
        self.assertEqual(failed["local_publish"], "succeeded")
        self.remote.failure = ""
        retry = self.activate()
        self.assertEqual(retry["release_id"], failed["release_id"])
        self.assertFalse(retry["bundle_generated"])
        self.assertEqual(retry["deployment"]["status"], "succeeded")

    def test_uncertain_submit_status_lookup_and_retry_without_duplicate(self):
        item = self.store.create_content("首次", "正文", visibility="public")
        self.configure()
        self.remote.failure = "create_response"
        self.assertEqual(self.activate()["deployment"]["status"], "unconfirmed")
        self.assertEqual(self.publish().status_code, 422)
        self.reopen()
        self.remote.failure = ""
        checked = self.post("/api/publish/cloudflare/check").get_json()["data"]
        self.assertEqual(checked["status"], "succeeded")
        self.assertEqual(self.remote.creates, 1)
        self.store.update_content(item["id"], item["revision"], title="再次更新")
        self.remote.failure = "poll"
        self.assertEqual(self.activate()["deployment"]["status"], "unconfirmed")
        self.assertEqual(self.remote.creates, 2)
        self.remote.failure = ""
        self.assertEqual(self.post("/api/publish/cloudflare/check").get_json()["data"]["status"], "succeeded")
        self.assertEqual(self.remote.creates, 2)

    def test_oversized_attachment_is_rejected_before_local_activation(self):
        item = self.store.create_content("超限附件", "正文", visibility="public")
        uploaded = self.owner.post(
            f"/api/content/{item['id']}/attachments", headers=self.csrf,
            data={"file": (io.BytesIO(b"x" * (25 * 1024 * 1024 + 1)), "large.txt"),
                  "operation_id": "pages_oversized_probe", "visibility": "public"},
        )
        self.assertEqual(uploaded.status_code, 201)
        self.configure()
        self.assertEqual(self.publish().status_code, 422)
        self.assertIsNone(self.get("/api/publish/plan")["active_release_id"])
        self.assertEqual(self.remote.creates, 0)

    def test_bad_token_missing_project_and_offline_preserve_local_publish(self):
        self.assertEqual(self.configure("incorrect-bearer-token-123456").status_code, 422)
        self.remote.failure = "missing_project"
        self.assertEqual(self.configure().status_code, 422)
        self.remote.failure = ""
        self.configure()
        self.store.create_content("待公开", "正文", visibility="public")
        self.remote.failure = "offline"
        result = self.activate()
        self.assertEqual(result["local_publish"], "succeeded")
        self.assertEqual(result["deployment"]["status"], "failed")
        self.assertEqual(self.remote.creates, 0)
        self.assertIsNone(result["deployment"]["public_url"])

    def test_public_attachment_and_deleted_content_are_checked(self):
        keep = self.store.create_content("继续公开", "正文", visibility="public")
        drop = self.store.create_content("随后删除", "正文", visibility="public")
        for suffix, visibility in [("public", "public"), ("private", "private")]:
            uploaded = self.owner.post(
                f"/api/content/{keep['id']}/attachments", headers=self.csrf,
                data={"file": (io.BytesIO(suffix.encode()), suffix + ".txt"),
                      "operation_id": "pages_sy_" + suffix, "visibility": visibility},
            )
            self.assertEqual(uploaded.status_code, 201)
        self.configure()
        self.assertEqual(self.activate()["deployment"]["status"], "succeeded")
        self.assertIn(b"Content-Disposition: attachment", self.remote.headers)
        paths = self.remote.files[self.remote.canonical]
        self.assertTrue(any(path.endswith("/public.txt") for path in paths))
        self.assertFalse(any(path.endswith("/private.txt") for path in paths))
        self.store.delete_content(drop["id"], drop["revision"])
        self.assertIn(drop["id"], self.get("/api/publication/state")["deployment"]["pending_removal_ids"])
        self.assertEqual(self.activate(remove=True)["deployment"]["status"], "unconfirmed")
        pending = self.check()
        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(self.approve(pending["verification"]["aliases"][0]).status_code, 200)
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(len(self.remote.deleted), 1)


    def test_attachment_update_retirement_and_explicit_alias_cleanup(self):
        old, path, before = self.replaced_attachment()
        started = self.activate()["deployment"]
        self.assertEqual(started["status"], "unconfirmed")
        self.assertTrue(started["verification"]["canonical_confirmed"])
        self.assertTrue(started["verification"]["positive_verified"])
        successful = [row for row in started["verification"]["recent_observations"]
                      if row["method"] == "GET" and row["http_status"] == 200]
        self.assertIn("/", {row["path"] for row in successful})
        self.assertTrue(any(row["path"].startswith("/content/") for row in successful))
        new = next(row for row in successful if row["path"].startswith("/attachments/"))
        self.assertNotEqual(new["path"], "/" + path)
        self.assertEqual(new["sha256"], hashlib.sha256(b"synthetic new payload").hexdigest())
        ready = self.check()
        self.assertEqual(ready["status"], "cleanup_pending")
        self.assertEqual(self.remote.delete_attempts, [])
        candidate = ready["verification"]["aliases"][0]
        self.assertEqual(candidate["id"], old)
        self.assertEqual(candidate["status"], "accessible")
        self.assertEqual(candidate["created_at"], "2026-11-21T14:14:02+00:00")
        self.assertEqual(candidate["origin"], "application_marker")
        self.assertEqual(self.approve(candidate, confirm=False).status_code, 409)
        self.assertEqual(self.approve(candidate, approval="incorrect").status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [])
        self.assertEqual(self.approve(candidate).status_code, 200)
        self.reopen()
        finished = self.check()
        self.assertEqual(finished["status"], "succeeded")
        self.assertTrue(finished["verification"]["complete"])
        self.assertEqual(finished["verification"]["aliases"][0]["status"], "retired")
        self.assertEqual(self.approve(candidate).status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [old])
        self.assertEqual(self.remote.creates, 2)
        self.assertTrue(self.stored()[0]["deleted"])
        self.assertNotEqual(hashlib.sha256(before).hexdigest(), new["sha256"])

    def test_tpe_old_bytes_remain_blocking_until_tpe_itself_is_reverified(self):
        _, path, before = self.replaced_attachment()
        cache = {"CF-Cache-Status": "HIT", "Age": "673", "Set-Cookie": TOKEN}
        for method in ("HEAD", "GET"):
            self.remote.public_overrides["local", DOMAIN, path, method] = (
                200, cache, before if method == "GET" else b"",
            )
        pending = self.activate()["deployment"]
        self.assertEqual(pending["status"], "unconfirmed")
        self.assertTrue(pending["verification"]["positive_verified"])
        self.assertEqual(pending["verification"]["blocking_pops"], ["TPE"])
        rows = [row for row in pending["verification"]["recent_observations"] if row["path"] == "/" + path]
        self.assertIn(("LAX", "GET", 404), [(row["pop"], row["method"], row["http_status"]) for row in rows])
        stale = next(row for row in rows if row.get("old_bytes"))
        self.assertEqual(stale["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(stale["ray_id"], "012345…-TPE")
        self.assertEqual(stale["cache_status"], "HIT")
        self.assertEqual(stale["age"], 673)
        self.assertNotIn(TOKEN, json.dumps(self.stored()))
        original_attempt = self.stored()[-1]["verification_job"]["attempts"][0]
        self.remote.public_overrides.clear()
        self.remote.pops["local"] = "SEA"
        self.reopen()
        for _ in range(2):
            self.assertEqual(self.check()["status"], "unconfirmed")
        self.remote.pops["local"] = "TPE"
        self.assertEqual(self.check()["status"], "unconfirmed")
        self.assertEqual(self.check()["status"], "cleanup_pending")
        self.assertEqual(self.stored()[-1]["verification_job"]["attempts"][0], original_attempt)
        self.assertEqual(self.remote.creates, 2)
        self.assertEqual(self.remote.delete_attempts, [])

    def test_head_and_get_must_both_show_absence(self):
        _, path, before = self.replaced_attachment()
        for head, get in [(404, 200), (200, 404), (403, 404), (404, 410), (302, 404), (404, 503)]:
            with self.subTest(head=head, get=get):
                self.remote.public_overrides["local", DOMAIN, path, "HEAD"] = (head, {}, b"")
                self.remote.public_overrides["local", DOMAIN, path, "GET"] = (
                    get, {}, before if get == 200 else b"",
                )
                state = self.activate()["deployment"] if self.remote.creates == 1 else self.check()
                self.assertEqual(state["status"], "unconfirmed")
                self.assertFalse(state["verification"]["production_retired"])
        self.remote.public_overrides["local", DOMAIN, path, "HEAD"] = PagesError("模拟超时", timeout=True)
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = (404, {}, b"")
        state = self.check()
        self.assertTrue(any(row.get("error") == "timeout" for row in state["verification"]["recent_observations"]))
        self.assertEqual(self.remote.delete_attempts, [])

    def test_one_exit_or_two_exits_in_one_pop_cannot_establish_retirement(self):
        self.replaced_attachment()
        hosting = self.app.extensions["pages_hosting"]
        hosting.probe_proxy = None
        self.assertEqual(self.activate()["deployment"]["status"], "unconfirmed")
        for _ in range(2):
            self.assertEqual(self.check()["verification"]["clean_rounds"], 0)
        self.reopen()
        self.remote.pops["independent"] = "TPE"
        for _ in range(2):
            self.assertEqual(self.check()["verification"]["clean_rounds"], 0)
        self.assertEqual(self.remote.creates, 2)

    def test_cache_evidence_error_timeout_unknown_and_false_completion(self):
        _, path, before = self.replaced_attachment()
        self.remote.cache_result = PagesError("模拟缓存处置失败 " + TOKEN)
        state = self.activate()["deployment"]
        self.assertEqual(state["verification"]["cache_status"], "FAILED")
        self.remote.cache_result = TimeoutError(TOKEN)
        state = self.check()
        self.assertEqual(state["verification"]["cache_status"], "TIMEOUT")
        self.assertEqual(state["status"], "unconfirmed")
        self.remote.cache_result = "UNKNOWN"
        self.assertEqual(self.check()["status"], "unconfirmed")
        self.remote.cache_result = "COMPLETED"
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = (200, {"CF-Cache-Status": "HIT"}, before)
        # Reopen forces an independent observation of the public URL, even if cached
        # verification progress previously reached the required number of rounds.
        self.reopen()
        state = self.check()
        self.assertEqual(state["verification"]["cache_status"], "COMPLETED")
        self.assertFalse(state["verification"]["complete"])
        self.assertEqual(state["status"], "unconfirmed")
        self.assertNotIn(TOKEN, json.dumps(self.stored()))

    def test_rollback_or_drift_cancels_cleanup_approval_and_never_republishes(self):
        old, path, _ = self.replaced_attachment()
        self.activate()
        ready = self.check()
        candidate = ready["verification"]["aliases"][0]
        successor = self.remote.canonical
        self.remote.canonical = old  # Simulated external rollback of the control plane.
        self.assertEqual(self.approve(candidate).status_code, 422)
        drift = self.check()
        self.assertTrue(drift["production_unknown"])
        self.assertFalse(drift["verification"]["canonical_confirmed"])
        self.assertIsNone(drift["public_url"])
        self.assertIn(path, self.stored()[-1]["verification_job"]["removed"])
        self.assertEqual(self.publish(force=True).status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [])
        self.assertEqual(self.remote.creates, 2)
        self.remote.canonical = successor
        self.assertEqual(self.check()["status"], "unconfirmed")
        self.assertEqual(self.check()["status"], "cleanup_pending")

    def test_unknown_origin_legacy_baseline_and_current_are_never_deleted(self):
        old, _, _ = self.replaced_attachment()
        hosting = self.app.extensions["pages_hosting"]
        data = hosting._load()
        data["records"][0].pop("retirement_managed")
        hosting._save(data)
        self.activate()
        pending = self.check()
        protected = pending["verification"]["aliases"][0]
        self.assertTrue(protected["protected"])
        self.assertIsNone(protected["approval"])
        self.assertEqual(self.approve(protected, approval="arbitrary").status_code, 422)
        self.assertEqual(self.approve(protected, deployment_id=self.remote.canonical, approval="arbitrary").status_code, 422)
        self.remote.deployments[old]["deployment_trigger"]["metadata"]["commit_message"] = "external"
        unowned = self.check()["verification"]["aliases"][0]
        self.assertEqual(unowned["status"], "unknown")
        self.assertIsNone(unowned["approval"])
        self.assertEqual(self.remote.delete_attempts, [])

    def test_interrupted_probe_restarts_and_never_repeats_the_submission(self):
        _, path, _ = self.replaced_attachment()
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = SystemExit("simulated interruption")
        with self.assertRaises(SystemExit):
            self.activate()
        current = self.remote.canonical
        job = self.stored()[-1]["verification_job"]
        self.assertEqual(job["attempts"][-1]["outcome"], "incomplete")
        self.assertIn(path, job["removed"])
        self.remote.public_overrides.clear()
        self.reopen()
        self.assertEqual(self.check()["status"], "unconfirmed")
        self.assertEqual(self.check()["status"], "cleanup_pending")
        self.assertEqual(self.remote.canonical, current)
        self.assertEqual(self.remote.creates, 2)

    def test_ambiguous_delete_never_repeats_and_alias_bytes_still_block(self):
        old, path, before = self.replaced_attachment()
        self.activate()
        ready = self.check()
        candidate = ready["verification"]["aliases"][0]
        self.remote.failure = "cleanup_reply"
        alias_host = candidate["host"]
        self.remote.public_overrides["local", alias_host, path, "GET"] = (200, {"CF-Cache-Status": "HIT"}, before)
        self.assertEqual(self.approve(candidate).status_code, 200)
        self.reopen()
        pending = self.check()
        self.assertEqual(pending["status"], "cleanup_pending")
        self.assertEqual(pending["verification"]["aliases"][0]["status"], "unconfirmed")
        self.assertEqual(self.approve(candidate).status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [old])
        self.remote.public_overrides.clear()
        self.assertEqual(self.check()["status"], "succeeded")
        self.assertEqual(self.remote.delete_attempts, [old])
        self.assertEqual(self.stored()[0]["delete_request"]["state"], "VERIFIED_ABSENT")

    def test_interrupted_reverification_invalidates_previously_collected_cleanup_proof(self):
        _, path, _ = self.replaced_attachment()
        self.activate()
        candidate = self.check()["verification"]["aliases"][0]
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = SystemExit("simulated interruption")
        with self.assertRaises(SystemExit):
            self.check()
        self.remote.public_overrides.clear()
        self.reopen()
        self.assertEqual(self.approve(candidate).status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [])
        self.assertEqual(self.check()["status"], "cleanup_pending")

    def test_cleanup_approval_does_not_delete_if_old_production_bytes_reappear(self):
        _, path, before = self.replaced_attachment()
        self.activate()
        ready = self.check()
        candidate = ready["verification"]["aliases"][0]
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = (200, {}, before)
        response = self.approve(candidate)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.remote.delete_attempts, [])
        pending = self.get("/api/publish/plan")["deployment"]
        self.assertEqual(pending["status"], "unconfirmed")
        self.assertEqual(pending["verification"]["blocking_pops"], ["TPE"])

    def test_confirmed_canonical_can_withdraw_while_older_attachment_still_hits_cache(self):
        control = self.store.create_content("保留条目", "公开控制正文", visibility="public")
        _, path, before = self.replaced_attachment()
        item_id = next(identifier for identifier in self.stored()[0]["content_ids"] if identifier != control["id"])
        self.remote.public_overrides["local", DOMAIN, path, "GET"] = (200, {"CF-Cache-Status": "HIT"}, before)
        a3 = self.activate()["deployment"]
        self.assertEqual(a3["status"], "unconfirmed")
        self.assertTrue(a3["can_publish_withdrawal_successor"])
        a3_record = self.stored()[-1]["verification_job"]
        item = self.store.get_content(item_id)
        self.store.update_content(item_id, item["revision"], visibility="private")
        self.assertEqual(self.publish().status_code, 422)
        self.assertEqual(self.publish(force=True, remove=True).status_code, 422)
        self.assertEqual(self.remote.creates, 2)
        a4 = self.activate(remove=True)["deployment"]
        self.assertEqual(a4["status"], "unconfirmed")
        self.assertTrue(a4["verification"]["canonical_confirmed"])
        self.assertTrue(a4["verification"]["positive_verified"])
        self.assertEqual(self.remote.creates, 3)
        self.assertEqual(self.remote.delete_attempts, [])
        history = self.stored()
        self.assertEqual(history[-2]["verification_job"], a3_record)
        self.assertEqual(history[-1]["content_ids"], [control["id"]])
        job = history[-1]["verification_job"]
        self.assertIn(path, job["removed"])
        self.assertIn("content/" + public_content_id(item_id) + ".html", job["removed"])
        self.assertIn("TPE", job["production"]["blocking"][path])
        self.reopen()
        self.assertEqual(self.check()["last_attempt"]["deployment_id"], a4["last_attempt"]["deployment_id"])

    def test_withdrawal_successor_cannot_add_content_or_advance_after_drift(self):
        _, _, _ = self.replaced_attachment()
        a3 = self.activate()["deployment"]["last_attempt"]["deployment_id"]
        history = self.stored()
        item = self.store.get_content(history[0]["content_ids"][0])
        self.store.update_content(item["id"], item["revision"], visibility="private")
        extra = self.store.create_content("不应顺带发布", "公开正文", visibility="public")
        self.assertEqual(self.publish(remove=True).status_code, 422)
        self.assertEqual(self.remote.creates, 2)
        self.store.update_content(extra["id"], extra["revision"], visibility="private")
        self.remote.canonical = history[0]["deployment_id"]
        result = self.activate(remove=True)["deployment"]
        self.assertTrue(result["production_unknown"])
        self.assertEqual(result["last_attempt"]["deployment_id"], a3)
        self.assertEqual(self.remote.creates, 2)
        self.assertEqual(self.remote.delete_attempts, [])

    def test_failed_withdrawal_upload_preserves_the_job_and_can_retry_local_successor(self):
        self.replaced_attachment()
        a3 = self.activate()["deployment"]["last_attempt"]["deployment_id"]
        item = self.store.get_content(self.stored()[0]["content_ids"][0])
        self.store.update_content(item["id"], item["revision"], visibility="private")
        self.remote.failure = "upload"
        pending = self.activate(remove=True)
        self.assertEqual(pending["local_publish"], "withdrawn")
        self.assertEqual(pending["deployment"]["last_attempt"]["deployment_id"], a3)
        self.assertEqual(self.stored()[-1]["verification_job"]["attempts"][-1]["outcome"], "withdrawal_submit_failed")
        self.assertEqual(self.remote.creates, 2)
        # A prior build may have persisted the failed, unsubmitted attempt as its latest row.
        hosting = self.app.extensions["pages_hosting"]
        data = hosting._load()
        data["records"].append({"project_id": PROJECT_ID, "status": "failed", "deployment_id": None,
                                "release_id": pending["release_id"]})
        hosting._save(data)
        self.reopen()
        self.remote.failure = ""
        a4 = self.activate(remove=True)
        self.assertEqual(a4["deployment"]["status"], "unconfirmed")
        self.assertNotEqual(a4["deployment"]["last_attempt"]["deployment_id"], a3)
        self.assertEqual(self.remote.creates, 3)
        self.assertEqual(self.remote.delete_attempts, [])

    def test_legacy_verifying_deployment_recovers_full_manifest_before_completion(self):
        _, path, _ = self.replaced_attachment()
        self.activate()
        hosting = self.app.extensions["pages_hosting"]
        data = hosting._load()
        current = data["records"][-1]
        current.pop("verification_job")
        current.pop("asset_facts")
        current["samples"] = {"index.html": current["samples"]["index.html"]}
        hosting._save(data)
        new_path = next(asset[1:] for asset in self.remote.files[self.remote.canonical]
                        if asset.startswith("/attachments/"))
        self.assertNotEqual(path, new_path)
        self.remote.public_overrides["local", DOMAIN, new_path, "GET"] = (200, {}, b"wrong bytes")
        self.reopen()
        pending = self.check()
        self.assertFalse(pending["verification"]["positive_verified"])
        self.assertEqual(pending["last_attempt"]["verification_error"]["kind"], "file_mismatch")
        self.assertIn(new_path, self.stored()[-1]["samples"])
        self.assertEqual(self.remote.creates, 2)


class LiveWindowsProjectDiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and os.environ.get("PDS_CLOUDFLARE_DISCOVERY_CONNECTION"),
                         "opt-in read-only Cloudflare Pages discovery")
    def test_saved_windows_credential_finds_acceptance_project(self):
        connection = Path(os.environ["PDS_CLOUDFLARE_DISCOVERY_CONNECTION"])
        saved = json.loads(connection.read_text(encoding="utf-8"))["target"]
        self.assertEqual(saved["project"], "pds-v1-acceptance")
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "data", bootstrap_token=OWNER, testing=True)
            hosting = app.extensions["pages_hosting"]
            hosting.path.parent.mkdir()
            hosting.path.write_text(json.dumps({"version": 1, "target": saved, "records": []}), encoding="utf-8")
            owner = app.test_client()
            owner.get("/auth?token=" + OWNER)
            with owner.session_transaction() as session:
                csrf = {"X-CSRF-Token": session["csrf"]}
            send_request = PagesClient._request
            endpoint = f"https://api.cloudflare.com/client/v4/accounts/{saved['account']}/pages/projects?"

            def read_only_list(client, method, url, **options):
                if method != "GET" or not url.startswith(endpoint):
                    self.fail("Live test may only list Cloudflare Pages projects")
                result = send_request(client, method, url, **options)
                self.assertEqual(result[0], 200, "Cloudflare did not accept the Pages list request")
                return result

            with patch.object(PagesClient, "_request", read_only_list):
                response = owner.post("/api/publish/cloudflare/projects",
                                      json={"account": saved["account"], "token": ""}, headers=csrf)
            self.assertEqual(response.status_code, 200, response.get_json().get("error"))
            self.assertIn("pds-v1-acceptance", response.get_json()["data"]["projects"])


class WindowsCredentialTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager")
    def test_roundtrip_with_synthetic_token(self):
        credentials = WindowsCredentials()
        key = "PersonalDigitalSpace/Test/" + uuid4().hex
        self.addCleanup(credentials.delete, key)
        credentials.save(key, TOKEN)
        self.assertEqual(credentials.read(key), TOKEN)
        credentials.delete(key)
        with self.assertRaises(PagesError):
            credentials.read(key)


if __name__ == "__main__":
    unittest.main()
