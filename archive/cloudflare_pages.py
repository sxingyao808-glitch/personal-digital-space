"""Cloudflare Pages Direct Upload transport and Windows credential storage."""

from __future__ import annotations

import base64
import ctypes as ct
import json
import os
import re
from ctypes import wintypes as wt
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from . import __version__
from .store import ArchiveError

API = "https://api.cloudflare.com/client/v4"
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_ASSETS = 20_000
PROJECTS_PER_PAGE = 10
MAX_PROJECT_PAGES = 10
MAX_ERROR_BYTES = 16 * 1024
PUBLIC_VERIFICATION_USER_AGENT = f"PersonalDigitalSpace/{__version__} (Cloudflare Pages verification)"
PUBLIC_ERROR_BYTES = 256
ACCOUNT = re.compile(r"[0-9a-f]{32}")
PROJECT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?")
DEPLOYMENT = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,256}")
CF_RAY = re.compile(r"[0-9a-fA-F]{16}(?:-[A-Z]{3})?")


class PagesError(ArchiveError):
    code = "cloudflare_pages"
    status = 422

    def __init__(self, message: str, *, http_status: int | None = None, **details):
        super().__init__(message, **details)
        self.http_status = http_status


def _error_details(status, headers, envelope, bearer):
    details = {"http_status": status, "errors": []}
    ray_id = headers.get("CF-Ray", "")
    if CF_RAY.fullmatch(ray_id):
        details["ray_id"] = ray_id
    errors = envelope.get("errors") if isinstance(envelope, dict) else None
    if not isinstance(errors, list):
        return details
    for error in errors[:5]:
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            continue
        entry = {"code": error["code"]}
        message = error.get("message")
        if isinstance(message, str):
            message = message.replace(bearer, "[已脱敏]")
            message = re.sub(r"""(?i)\bauthorization["']?\s*[:=][^\r\n]*""", "[已脱敏]", message)
            message = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[已脱敏]", message)
            message = re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[已脱敏]", message)
            entry["message"] = " ".join(message.split())[:240]
        details["errors"].append(entry)
    return details


class _Credential(ct.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ct.POINTER(ct.c_ubyte)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ct.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


class WindowsCredentials:
    """A generic, per-user Windows Credential Manager entry."""

    @staticmethod
    def _api():
        if os.name != "nt":
            raise PagesError("当前系统无法使用 Windows 安全凭据存储。")
        api = ct.WinDLL("Advapi32", use_last_error=True)
        api.CredWriteW.argtypes = (ct.POINTER(_Credential), wt.DWORD)
        api.CredWriteW.restype = wt.BOOL
        api.CredReadW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(ct.POINTER(_Credential)))
        api.CredReadW.restype = wt.BOOL
        api.CredDeleteW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD)
        api.CredDeleteW.restype = wt.BOOL
        api.CredFree.argtypes = (ct.c_void_p,)
        api.CredFree.restype = None
        return api

    def save(self, key: str, token: str) -> None:
        api = self._api()
        data = bytearray(token, "ascii")
        try:
            blob = (ct.c_ubyte * len(data)).from_buffer(data)
            entry = _Credential(Type=1, TargetName=key, CredentialBlobSize=len(data),
                                CredentialBlob=blob, Persist=2, UserName="Cloudflare Pages")
            if not api.CredWriteW(ct.byref(entry), 0):
                raise PagesError("Windows 凭据管理器无法保存凭据。")
        finally:
            data[:] = bytes(len(data))

    def delete(self, key: str) -> None:
        self._api().CredDeleteW(key, 1, 0)

    def read(self, key: str) -> str:
        api = self._api()
        pointer = ct.POINTER(_Credential)()
        if not api.CredReadW(key, 1, 0, ct.byref(pointer)):
            raise PagesError("Windows 凭据管理器中没有可用凭据，请重新连接。")
        try:
            entry = pointer.contents
            token = ct.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode("ascii", "replace")
            if not TOKEN.fullmatch(token):
                raise PagesError("已保存的凭据无效，请重新连接。")
            return token
        finally:
            api.CredFree(pointer)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def asset_hash(encoded: bytes, path: str) -> str:
    # Pages keys use the first 128 bits of BLAKE3(base64(bytes) + extension).
    from blake3 import blake3

    return blake3(encoded + Path(path).suffix[1:].encode("utf-8")).hexdigest(length=16)


def _form(fields: dict[str, str], headers: bytes) -> tuple[bytes, str]:
    boundary = "archive-" + uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode("utf-8")
        )
    parts.extend([
        (f'--{boundary}\r\nContent-Disposition: form-data; name="_headers"; filename="_headers"'
         '\r\nContent-Type: text/plain\r\n\r\n').encode("ascii"),
        headers, f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class PagesClient:
    def __init__(self, account: str, project: str, token: str):
        if not ACCOUNT.fullmatch(account) or not PROJECT.fullmatch(project) or not TOKEN.fullmatch(token):
            raise PagesError("请检查账户 ID、项目名称和 API Token 格式。")
        self.project_path = f"/accounts/{account}/pages/projects/{project}"
        self.token = token
        self.opener = build_opener(_NoRedirect())

    def _request(self, method, url, *, data=None, headers=None, limit=4 * 1024 * 1024, error_limit=0,
                 allow_truncated=False, keep_error_prefix=False):
        try:
            request = Request(url, data=data, method=method, headers=headers or {})
            with self.opener.open(request, timeout=15) as response:
                payload = response.read(limit + 1)
                if len(payload) > limit and not allow_truncated:
                    raise PagesError("收到超出预期大小的响应。")
                return response.status, response.headers, payload
        except HTTPError as error:
            with error:
                try:
                    payload = error.read(error_limit + 1) if error_limit else b""
                except (OSError, HTTPException):
                    if keep_error_prefix:
                        raise PagesError("无法读取公开核验响应。") from None
                    payload = b""
            return error.code, error.headers, payload if keep_error_prefix or len(payload) <= error_limit else b""
        except (URLError, OSError, ValueError, HTTPException) as error:
            reason = error.reason if isinstance(error, URLError) else error
            raise PagesError("网络连接中断或超时，请检查连接后重试。",
                             timeout=isinstance(reason, TimeoutError)) from None

    def api(self, method, path, *, payload=None, form=None, token=None):
        return self._api_response(method, path, payload=payload, form=form, token=token).get("result")

    def _api_response(self, method, path, *, payload=None, form=None, token=None, include_error_details=False):
        bearer = token or self.token
        headers = {"Authorization": "Bearer " + bearer, "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if form is not None:
            data, headers["Content-Type"] = form
        status, response_headers, body = self._request(
            method, API + path, data=data, headers=headers,
            error_limit=MAX_ERROR_BYTES if include_error_details else 0,
        )
        try:
            envelope = json.loads(body)
        except (ValueError, RecursionError):
            envelope = None
        if 200 <= status < 300 and isinstance(envelope, dict) and envelope.get("success") is True:
            return envelope
        message = {
            401: "Cloudflare 凭据无效或权限不足。",
            403: "Cloudflare 凭据无效或权限不足。",
            404: "Cloudflare 项目或部署不存在。",
        }.get(status, "Cloudflare API 请求未成功。")
        details = {"cloudflare": _error_details(status, response_headers, envelope, bearer)} if include_error_details else {}
        raise PagesError(message, http_status=status, **details)

    def projects(self, account: str) -> list[str]:
        names = set()
        for page in range(1, MAX_PROJECT_PAGES + 1):
            envelope = self._api_response(
                "GET", f"/accounts/{account}/pages/projects?per_page={PROJECTS_PER_PAGE}&page={page}",
                include_error_details=True,
            )
            projects = envelope.get("result")
            if not isinstance(projects, list) or any(not isinstance(project, dict) for project in projects):
                raise PagesError("Cloudflare 未返回有效的项目列表。")
            for project in projects:
                name = project.get("name")
                if isinstance(name, str) and PROJECT.fullmatch(name) and not project.get("source"):
                    names.add(name)
            info = envelope.get("result_info")
            total_pages = info.get("total_pages") if isinstance(info, dict) else None
            if type(total_pages) is int and total_pages >= 0:
                is_last_page = page >= total_pages
            else:
                is_last_page = len(projects) < PROJECTS_PER_PAGE
            if is_last_page:
                return sorted(names)
        raise PagesError(f"Cloudflare 项目列表仍有后续页面，已达到 {MAX_PROJECT_PAGES} 页的安全上限，未能完整获取。")

    def project(self) -> dict:
        project = self.api("GET", self.project_path)
        if not isinstance(project, dict) or project.get("source"):
            raise PagesError("请选择使用直接上传的 Cloudflare Pages 项目。")
        domain = project.get("subdomain")
        if not isinstance(domain, str) or not domain.endswith(".pages.dev") or not PROJECT.fullmatch(domain[:-10]):
            raise PagesError("Cloudflare 未返回有效的公开网址。")
        branch = project.get("production_branch")
        if not isinstance(branch, str) or not branch:
            raise PagesError("Cloudflare 项目缺少生产分支。")
        return project

    def upload_token(self) -> str:
        result = self.api("GET", self.project_path + "/upload-token")
        jwt = result.get("jwt") if isinstance(result, dict) else None
        if not isinstance(jwt, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", jwt):
            raise PagesError("Cloudflare 未返回有效的上传授权。")
        return jwt

    def upload(self, assets, jwt: str) -> dict[str, str]:
        keys = {}
        by_key = {}
        for path, asset in assets.items():
            key = asset_hash(base64.b64encode(asset.read()), path)
            if key not in by_key or by_key[key].mime == "application/octet-stream":
                by_key[key] = asset
            keys["/" + path] = key
        missing = self.api("POST", "/pages/assets/check-missing", payload={"hashes": list(by_key)}, token=jwt)
        if not isinstance(missing, list) or any(not isinstance(key, str) or key not in by_key for key in missing):
            raise PagesError("Cloudflare 返回的待上传资源列表无效。")
        batch = []
        size = 0
        for key in dict.fromkeys(missing):
            asset = by_key[key]
            if batch and (size + asset.size > MAX_ASSET_BYTES or len(batch) >= 200):
                self.api("POST", "/pages/assets/upload", payload=batch, token=jwt)
                batch, size = [], 0
            encoded = base64.b64encode(asset.read())
            batch.append({"key": key, "value": encoded.decode("ascii"),
                          "metadata": {"contentType": asset.mime}, "base64": True})
            size += asset.size
        if batch:
            self.api("POST", "/pages/assets/upload", payload=batch, token=jwt)
        self.api("POST", "/pages/assets/upsert-hashes", payload={"hashes": list(by_key)}, token=jwt)
        return keys

    def create_deployment(self, manifest, branch, marker, headers):
        fields = {"manifest": json.dumps(manifest), "branch": branch, "commit_message": marker}
        return self.api("POST", self.project_path + "/deployments", form=_form(fields, headers))

    def deployment(self, deployment_id: str):
        if not DEPLOYMENT.fullmatch(deployment_id):
            raise PagesError("Cloudflare 部署编号无效。")
        result = self.api("GET", self.project_path + "/deployments/" + deployment_id)
        if not isinstance(result, dict):
            raise PagesError("Cloudflare 未返回有效的部署状态。")
        return result

    def find_deployment(self, marker: str) -> str | None:
        for page in range(1, 11):
            items = self.api("GET", self.project_path + f"/deployments?per_page=25&page={page}")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise PagesError("Cloudflare 未返回有效的部署历史。")
            for item in items:
                metadata = (item.get("deployment_trigger") or {}).get("metadata") or {}
                deployment_id = item.get("id") or ""
                if metadata.get("commit_message") == marker and DEPLOYMENT.fullmatch(deployment_id):
                    return deployment_id
            if len(items) < 25:
                break
        return None

    def delete_deployment(self, deployment_id: str) -> None:
        if not DEPLOYMENT.fullmatch(deployment_id):
            raise PagesError("Cloudflare 部署编号无效。")
        try:
            self.api("DELETE", self.project_path + "/deployments/" + deployment_id)
        except PagesError as error:
            if error.http_status != 404:
                raise
        try:
            self.deployment(deployment_id)
        except PagesError as error:
            if error.http_status == 404:
                return
            raise
        raise PagesError("旧部署尚未从 Cloudflare 移除。")

    def public(self, domain: str, path: str, *, limit: int = 0, method=None, error_limit=PUBLIC_ERROR_BYTES):
        method = method or ("GET" if limit else "HEAD")
        return self._public_request(domain, path, method=method, limit=limit, error_limit=error_limit)

    def public_probe(self, proxy_url: str):
        """Build an independent anonymous HTTPS probe without sharing the API client or its token."""
        try:
            proxy = urlsplit(proxy_url)
            valid = (proxy.scheme == "http" and proxy.hostname and proxy.port and not proxy.username
                     and proxy.password is None and proxy.path in ("", "/") and not proxy.query and not proxy.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise PagesError("独立核验出口须为不含凭据的 HTTP CONNECT 代理。")
        return self._anonymous_probe(ProxyHandler({"https": proxy_url}))

    def direct_public_probe(self):
        """Provide the no-proxy leg of an explicitly configured two-exit check."""
        return self._anonymous_probe(ProxyHandler({}))

    @staticmethod
    def _anonymous_probe(proxy_handler):
        probe = object.__new__(PagesClient)
        probe.opener = build_opener(proxy_handler, _NoRedirect())
        return probe.public

    def _public_request(self, domain, path, *, method, limit, error_limit):
        if not re.fullmatch(r"(?:[a-z0-9-]+\.){1,2}pages\.dev", domain):
            raise PagesError("公开网址无效。")
        if method not in ("GET", "HEAD"):
            raise PagesError("公开核验仅支持匿名 HEAD 和 GET。")
        return self._request(
            method, f"https://{domain}/" + quote(path, safe="/"),
            headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache",
                     "User-Agent": PUBLIC_VERIFICATION_USER_AGENT},
            limit=limit, error_limit=error_limit, allow_truncated=True,
            keep_error_prefix=error_limit != PUBLIC_ERROR_BYTES,
        )
