"""Flask application: loopback-only local Web UI and JSON API."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from functools import wraps
from pathlib import Path
from threading import RLock
from tempfile import TemporaryDirectory
from typing import Any, Callable

from werkzeug.exceptions import HTTPException

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import __version__
from .attachments import AttachmentStore
from .win_data import make_data_dir
from .auth import ensure_secret, host_allowed, origin_allowed, token_matches
from .cloudflare_pages import PagesError
from .db import init_db
from .pages_hosting import PagesHosting
from .export import ArchiveExporter
from .release import (
    PUBLISH_OPERATIONS,
    RELEASE_STATUSES,
    ReleaseConfirmationError,
    ReleaseConflictError,
    ReleaseRegistry,
    ReleaseValidationError,
)
from .release_reader import (
    ActiveReleaseNotFound,
    ActiveReleaseUnavailable,
    ActiveReleaseReader,
)
from .store import (
    ArchiveError,
    ArchiveStore,
    NotFoundError,
    ValidationError,
)

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

WEB_ROOT = Path(__file__).parent / "web"


def create_app(
    data_dir: str | Path,
    *,
    bootstrap_token: str | None = None,
    testing: bool = False,
) -> Flask:
    data_dir = Path(data_dir)
    make_data_dir(data_dir)
    init_db(data_dir / "archive.db")

    app = Flask(
        __name__,
        static_folder=str(WEB_ROOT / "static"),
        static_url_path="/static",
        template_folder=str(WEB_ROOT),
    )
    app.config.update(
        SECRET_KEY=ensure_secret(data_dir),
        SESSION_COOKIE_NAME="archive_owner",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=55 * 1024 * 1024,
        TESTING=testing,
        PROPAGATE_EXCEPTIONS=testing,
    )

    owner_write_lock = RLock()
    store = ArchiveStore(data_dir)
    attachments = AttachmentStore(data_dir)
    exporter = ArchiveExporter(data_dir)
    releases = ReleaseRegistry(data_dir)
    release_reader = ActiveReleaseReader(data_dir)
    hosting = PagesHosting(data_dir, releases, CSP)
    draft_site = None
    app.extensions["pages_hosting"] = hosting
    token = bootstrap_token or secrets.token_urlsafe(32)
    app.config["ARCHIVE_BOOTSTRAP_TOKEN"] = token
    app.config["ARCHIVE_DATA_DIR"] = str(data_dir)
    app.extensions["archive_store"] = store
    app.extensions["attachment_store"] = attachments
    app.extensions["archive_exporter"] = exporter
    app.extensions["release_registry"] = releases
    app.extensions["release_reader"] = release_reader
    # Best-effort startup audit: it only reads state and writes a traceable
    # report; public routes fail closed independently when active is invalid.
    app.extensions["release_startup_audit"] = releases.startup_audit()

    # ------------------------------------------------------------------
    # Request guards
    # ------------------------------------------------------------------
    @app.before_request
    def guard_host_and_session() -> Any:
        if not host_allowed(request.host):
            return jsonify(
                {"ok": False, "error": {"code": "host_rejected", "message": "请求 Host 不被允许。"}}
            ), 403
        if session.get("owner") and not session.get("csrf"):
            session["csrf"] = secrets.token_urlsafe(32)
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.path.startswith(
            ("/api/", "/p/", "/assets/", "/attachments/", "/content/", "/spaces/", "/preview/", "/preview-draft/")
        ) or request.path in ("/", "/index.html", "/releases", "/login", "/auth"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    def _login_owner() -> None:
        session.clear()
        session.permanent = True
        session["owner"] = True
        session["csrf"] = secrets.token_urlsafe(32)

    def owner_page(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("owner"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def owner_api(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("owner"):
                return jsonify(
                    {"ok": False, "error": {"code": "unauthorized", "message": "需要所有者身份。"}}
                ), 401
            if request.method in ("POST", "PATCH", "PUT", "DELETE"):
                csrf = request.headers.get("X-CSRF-Token")
                expected_csrf = session.get("csrf")
                if not (
                    csrf
                    and csrf.isascii()
                    and isinstance(expected_csrf, str)
                    and expected_csrf.isascii()
                    and hmac.compare_digest(csrf, expected_csrf)
                ):
                    return jsonify(
                        {"ok": False, "error": {"code": "csrf_failed", "message": "CSRF 校验失败。"}}
                    ), 403
                if not origin_allowed(
                    request.headers.get("Origin"), request.scheme, request.host
                ):
                    return jsonify(
                        {
                            "ok": False,
                            "error": {"code": "origin_rejected", "message": "请求来源不被允许。"},
                        }
                    ), 403
                with owner_write_lock:
                    return view(*args, **kwargs)
            return view(*args, **kwargs)

        return wrapped

    def ok(data: Any, status: int = 200):
        return jsonify({"ok": True, "data": data}), status

    @app.errorhandler(ArchiveError)
    def handle_archive_error(exc: ArchiveError):
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
        return jsonify({"ok": False, "error": error}), exc.status

    @app.errorhandler(413)
    def handle_too_large(_exc):
        return jsonify(
            {"ok": False, "error": {"code": "too_large", "message": "上传内容超过大小上限。"}}
        ), 413

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(
                {
                    "ok": False,
                    "error": {
                        "code": exc.name.lower().replace(" ", "_"),
                        "message": exc.description,
                    },
                }
            ), exc.code or 404
        return render_template("not_found.html", version=__version__), exc.code or 404

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        if isinstance(exc, ArchiveError):
            return handle_archive_error(exc)
        app.logger.exception("Unhandled request error")
        if request.path.startswith("/api/"):
            return jsonify(
                {
                    "ok": False,
                    "error": {"code": "internal_error", "message": "服务发生未预期错误。"},
                }
            ), 500
        raise exc

    # ------------------------------------------------------------------
    # Pages and authentication
    # ------------------------------------------------------------------
    @app.get("/login")
    def login():
        if session.get("owner"):
            return redirect(url_for("index"))
        return render_template("login.html", error=None, version=__version__)

    @app.post("/login")
    def login_submit():
        candidate = request.form.get("token", "")
        if token_matches(candidate, app.config["ARCHIVE_BOOTSTRAP_TOKEN"]):
            _login_owner()
            return redirect(url_for("index"))
        return render_template(
            "login.html", error="登录令牌无效。请使用启动时打印的地址。", version=__version__
        ), 403

    @app.get("/auth")
    def auth():
        candidate = request.args.get("token", "")
        if token_matches(candidate, app.config["ARCHIVE_BOOTSTRAP_TOKEN"]):
            _login_owner()
            return redirect(url_for("index"))
        return redirect(url_for("login"))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def _bundle_response(relative: str, *, redirect_when_missing: bool = False):
        """Serve one verified file from the active public bundle."""
        try:
            path = release_reader.bundle_file(relative)
        except ActiveReleaseNotFound:
            if redirect_when_missing:
                return redirect(url_for("login"))
            return render_template("not_found.html", version=__version__), 404
        except ActiveReleaseUnavailable:
            return render_template("not_found.html", version=__version__), 503
        return send_file(path, mimetype="text/html", max_age=0)

    @app.get("/")
    def index():
        if not session.get("owner"):
            return public_home()
        return render_template(
            "index.html",
            csrf=session.get("csrf", ""),
            version=__version__,
        )

    def public_home():
        """Anonymous public site home (bundle ``index.html``, never a template)."""
        return _bundle_response("index.html", redirect_when_missing=True)

    @app.get("/index.html")
    def public_home_file():
        return _bundle_response("index.html")

    @app.get("/content/<name>")
    def public_content_file(name: str):
        return _bundle_response(f"content/{name}")

    @app.get("/spaces/<space>/", strict_slashes=False)
    def public_space(space: str):
        return _bundle_response(f"spaces/{space}/index.html")

    @app.get("/releases")
    @owner_page
    def releases_page():
        return render_template(
            "releases.html",
            csrf=session.get("csrf", ""),
            version=__version__,
        )

    def preview_html(markup: bytes, label: str):
        markup = markup.replace(b"<body>", b'<body class="has-preview-banner">', 1)
        banner = f'<div class="public-preview-banner">{label}</div>'.encode("utf-8")
        response = make_response(markup.replace(b"</body>", banner + b"</body>", 1))
        response.mimetype = "text/html"
        return response

    def _preview_response(relative: str):
        try:
            path = release_reader.bundle_file(relative)
        except ActiveReleaseNotFound:
            return render_template("not_found.html", version=__version__), 404
        except ActiveReleaseUnavailable:
            return render_template("not_found.html", version=__version__), 503
        if relative.startswith("attachments/"):
            return send_file(path, mimetype="application/octet-stream", as_attachment=True, max_age=0)
        if path.suffix == ".html":
            return preview_html(path.read_bytes(), "当前本机网站预览")
        mimetype = "text/css" if path.suffix == ".css" else None
        return send_file(path, mimetype=mimetype, max_age=0)

    @app.get("/preview/")
    @owner_page
    def preview_home():
        return _preview_response("index.html")

    @app.get("/preview/<path:relpath>")
    @owner_page
    def preview_file(relpath: str):
        return _preview_response(relpath)

    @app.get("/preview-draft/<nonce>/", defaults={"relative": ""})
    @app.get("/preview-draft/<nonce>/<path:relative>")
    @owner_page
    def draft_preview_file(nonce: str, relative: str):
        with owner_write_lock:
            if not draft_site or draft_site["nonce"] != nonce:
                return render_template("not_found.html", version=__version__), 404
            if not relative or relative.endswith("/"):
                relative += "index.html"
            if relative not in draft_site["paths"]:
                return render_template("not_found.html", version=__version__), 404
            path = draft_site["root"] / relative
            if relative.startswith("attachments/"):
                return send_file(path, mimetype="application/octet-stream", as_attachment=True, max_age=0)
            if path.suffix == ".html":
                return preview_html(path.read_bytes(), "待发布的网站预览")
            return send_file(path, mimetype="text/css", max_age=0)

    @app.get("/p/<content_id>")
    def public_page(content_id: str):
        """Public page: resolved exclusively from the active Release Bundle."""
        identifier = content_id[:-5] if content_id.endswith(".html") else content_id
        try:
            page_path = release_reader.content_page_path(identifier)
        except ActiveReleaseNotFound:
            return render_template("not_found.html", version=__version__), 404
        return send_file(page_path, mimetype="text/html", max_age=0)

    @app.get("/assets/<path:asset_path>")
    def public_asset(asset_path: str):
        try:
            path = release_reader.asset_path(f"assets/{asset_path}")
        except ActiveReleaseNotFound:
            raise NotFoundError("公开资源不存在。")
        mimetype = "text/css" if path.suffix == ".css" else None
        return send_file(path, mimetype=mimetype, max_age=0)

    @app.get("/attachments/<attachment_id>/<filename>")
    def public_attachment(attachment_id: str, filename: str):
        try:
            path, attachment = release_reader.attachment_path(attachment_id, filename)
        except ActiveReleaseNotFound:
            raise NotFoundError("附件不存在。")
        return send_file(
            path,
            as_attachment=True,
            download_name=attachment.get("source_filename") or filename,
            mimetype=attachment.get("mime") or "application/octet-stream",
        )

    @app.get("/favicon.ico")
    def favicon():
        return send_file(WEB_ROOT / "static" / "favicon.ico", mimetype="image/x-icon")

    @app.get("/api/health")
    def health():
        return ok({"status": "ok", "version": __version__})

    @app.get("/api/session")
    @owner_api
    def session_info():
        integrity = attachments.get_integrity()
        return ok(
            {
                "owner": True,
                "csrf": session.get("csrf"),
                "version": __version__,
                "data_dir": app.config["ARCHIVE_DATA_DIR"],
                "integrity": {
                    "open_issue_count": integrity["active_open_issue_count"],
                    "quarantine_history_count": integrity["quarantine_history_count"],
                    "last_report": integrity["last_report"],
                },
            }
        )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    @app.get("/api/overview")
    @owner_api
    def overview():
        return ok(store.overview())

    @app.get("/api/content")
    @owner_api
    def list_content():
        query = request.args.get("q", "")
        include_deleted = request.args.get("deleted") == "1"
        return ok(
            {
                "items": store.list_content(
                    query=query,
                    include_deleted=include_deleted,
                    space=request.args.get("space") or None,
                    content_type=request.args.get("type") or None,
                    lifecycle=request.args.get("lifecycle") or None,
                )
            }
        )

    @app.post("/api/content")
    @owner_api
    def create_content():
        payload = request.get_json(silent=True) or {}
        content = store.create_content(
            title=payload.get("title", ""),
            body=payload.get("body", ""),
            visibility=payload.get("visibility"),
            summary=payload.get("summary", ""),
            content_type=payload.get("type", "THOUGHT"),
            primary_space=payload.get("primary_space", "ME"),
            lifecycle=payload.get("lifecycle", "DRAFT"),
            related_spaces=payload.get("related_spaces"),
        )
        return ok(content, 201)

    @app.get("/api/content/<content_id>")
    @owner_api
    def get_content(content_id: str):
        include_deleted = request.args.get("include_deleted") == "1"
        return ok(store.get_content(content_id, include_deleted=include_deleted))

    @app.patch("/api/content/<content_id>")
    @owner_api
    def update_content(content_id: str):
        payload = request.get_json(silent=True) or {}
        if "revision" not in payload:
            raise ValidationError("缺少 revision，无法安全更新。", field="revision")
        kwargs: dict[str, Any] = {}
        if "title" in payload:
            kwargs["title"] = payload["title"]
        if "body" in payload:
            kwargs["body"] = payload["body"]
        if "visibility" in payload:
            kwargs["visibility"] = payload["visibility"]
        if "summary" in payload:
            kwargs["summary"] = payload["summary"]
        if "type" in payload:
            kwargs["content_type"] = payload["type"]
        if "primary_space" in payload:
            kwargs["primary_space"] = payload["primary_space"]
        if "lifecycle" in payload:
            kwargs["lifecycle"] = payload["lifecycle"]
        if "related_spaces" in payload:
            kwargs["related_spaces"] = payload["related_spaces"]
        if not kwargs:
            raise ValidationError("没有可更新的字段。")
        return ok(store.update_content(content_id, payload["revision"], **kwargs))

    @app.delete("/api/content/<content_id>")
    @owner_api
    def delete_content(content_id: str):
        payload = request.get_json(silent=True) or {}
        if "revision" not in payload:
            raise ValidationError("缺少 revision，无法安全删除。", field="revision")
        return ok(store.delete_content(content_id, payload["revision"]))

    @app.post("/api/content/<content_id>/restore")
    @owner_api
    def restore_content(content_id: str):
        return ok(store.restore_content(content_id))

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------
    @app.post("/api/content/<content_id>/relations")
    @owner_api
    def add_relation(content_id: str):
        payload = request.get_json(silent=True) or {}
        if not payload.get("to_id") or not payload.get("type"):
            raise ValidationError("缺少关系端点或类型。")
        return ok(store.add_relation(content_id, payload["to_id"], payload["type"]), 201)

    @app.delete("/api/relations/<relation_id>")
    @owner_api
    def remove_relation(relation_id: str):
        return ok(store.delete_relation(relation_id))

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    @app.post("/api/content/<content_id>/attributes")
    @owner_api
    def add_attribute(content_id: str):
        payload = request.get_json(silent=True) or {}
        if "key" not in payload or "value" not in payload:
            raise ValidationError("缺少属性名称或值。")
        return ok(
            store.add_attribute(
                content_id,
                payload["key"],
                payload["value"],
                payload.get("provenance", "user"),
                confirmed=bool(payload.get("confirmed", False)),
                source_ref=payload.get("source_ref"),
            ),
            201,
        )

    @app.patch("/api/attributes/<attribute_id>")
    @owner_api
    def update_attribute(attribute_id: str):
        payload = request.get_json(silent=True) or {}
        kwargs: dict[str, Any] = {}
        for field in ("value", "provenance", "confirmed", "source_ref"):
            if field in payload:
                kwargs[field] = payload[field]
        if not kwargs:
            raise ValidationError("没有可更新的属性字段。")
        return ok(store.update_attribute(attribute_id, **kwargs))

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    @app.post("/api/content/<content_id>/attachments")
    @owner_api
    def upload_attachment(content_id: str):
        uploaded = request.files.get("file")
        if uploaded is None:
            raise ValidationError("缺少文件。", field="file")
        operation_id = request.form.get("operation_id", "")
        if not operation_id:
            raise ValidationError("缺少 operation_id。", field="operation_id")
        expected_size = request.form.get("expected_size")
        if expected_size not in (None, ""):
            try:
                expected_size_value = int(expected_size)
            except ValueError as exc:
                raise ValidationError("expected_size 必须是整数。", field="expected_size") from exc
        else:
            expected_size_value = None
        result = attachments.upload(
            uploaded.stream,
            filename=uploaded.filename or "attachment",
            content_id=content_id,
            operation_id=operation_id,
            attachment_id=request.form.get("attachment_id") or None,
            declared_mime=uploaded.mimetype,
            expected_sha256=request.form.get("expected_sha256") or None,
            expected_size=expected_size_value,
            visibility=request.form.get("visibility", "private"),
        )
        return ok(result, 200 if result.get("idempotent") else 201)

    @app.get("/api/attachments/<attachment_id>/file")
    def download_attachment(attachment_id: str):
        owner = bool(session.get("owner"))
        if not owner:
            # Anonymous downloads resolve the active Release Bundle only; the
            # private object store is never used as a public fallback.
            path, attachment = release_reader.attachment_path(attachment_id)
            return send_file(
                path,
                as_attachment=True,
                download_name=attachment.get("source_filename") or attachment_id,
                mimetype=attachment.get("mime") or "application/octet-stream",
            )
        path, filename, mime = attachments.download_target(attachment_id, owner=True)
        return send_file(path, as_attachment=True, download_name=filename, mimetype=mime)

    @app.patch("/api/attachments/<attachment_id>")
    @owner_api
    def update_attachment(attachment_id: str):
        payload = request.get_json(silent=True) or {}
        if "visibility" not in payload:
            raise ValidationError("缺少 visibility。", field="visibility")
        return ok(attachments.set_visibility(attachment_id, payload["visibility"]))

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------
    @app.get("/api/integrity")
    @owner_api
    def integrity():
        return ok(attachments.get_integrity())

    @app.post("/api/integrity/reconcile")
    @owner_api
    def reconcile():
        return ok(attachments.reconcile())

    # ------------------------------------------------------------------
    # Export (owner-only, never publishes)
    # ------------------------------------------------------------------
    @app.post("/api/export")
    @owner_api
    def create_export():
        payload = request.get_json(silent=True) or {}
        output_path = payload.get("output_path")
        if output_path is not None and not isinstance(output_path, str):
            raise ValidationError("output_path 必须是文本。", field="output_path")
        result = exporter.export(output_path)
        result["download_path"] = url_for("download_export", filename=result["filename"])
        return ok(result)

    @app.get("/api/export/<filename>")
    @owner_api
    def download_export(filename: str):
        path = exporter.resolve_download(filename)
        return send_file(
            path,
            as_attachment=True,
            download_name=path.name,
            mimetype="application/zip",
        )

    # ------------------------------------------------------------------
    # Controlled release lifecycle (P2: prepare, activate, withdraw, audit)
    # ------------------------------------------------------------------
    @app.get("/api/releases")
    @owner_api
    def list_releases():
        return ok(releases.list())

    @app.post("/api/releases/preview")
    @owner_api
    def preview_release():
        payload = request.get_json(silent=True) or {}
        content_ids = payload.get("content_ids")
        attachment_ids = payload.get("attachment_ids")
        if content_ids is not None and not isinstance(content_ids, list):
            raise ValidationError("content_ids 必须为数组。", field="content_ids")
        if attachment_ids is not None and not isinstance(attachment_ids, list):
            raise ValidationError("attachment_ids 必须为数组。", field="attachment_ids")
        return ok(releases.preview_build(content_ids, attachment_ids))

    @app.post("/api/releases/prepare")
    @owner_api
    def prepare_release():
        payload = request.get_json(silent=True) or {}
        result = releases.build(
            payload.get("content_ids"), payload.get("attachment_ids")
        )
        return ok(result, 201)

    @app.get("/api/releases/<release_id>")
    @owner_api
    def get_release(release_id: str):
        return ok(releases.get(release_id))

    @app.get("/api/releases/<release_id>/verify")
    @owner_api
    def verify_release(release_id: str):
        return ok(releases.verify(release_id))

    def _query_values(name: str) -> list[str]:
        values: list[str] = []
        for raw in request.args.getlist(name):
            values.extend(part.strip() for part in raw.split(",") if part.strip())
        return values

    @app.get("/api/releases/history")
    @owner_api
    def publishing_history():
        statuses = _query_values("status")
        actions = _query_values("action")
        for value in statuses:
            if value not in RELEASE_STATUSES:
                raise ValidationError(
                    "status 过滤值无效。", field="status", value=value
                )
        for value in actions:
            if value not in PUBLISH_OPERATIONS:
                raise ValidationError(
                    "action 过滤值无效。", field="action", value=value
                )
        try:
            limit = int(request.args.get("limit", "50"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("limit 必须是整数。", field="limit") from exc
        cursor = None
        raw_cursor = request.args.get("cursor")
        if raw_cursor:
            try:
                cursor = int(raw_cursor)
            except ValueError as exc:
                raise ValidationError("cursor 无效。", field="cursor") from exc
        return ok(
            releases.history(
                statuses=statuses,
                actions=actions,
                content_id=request.args.get("content_id"),
                since=request.args.get("since"),
                until=request.args.get("until"),
                order=request.args.get("order", "desc"),
                limit=limit,
                cursor=cursor,
            )
        )

    @app.get("/api/releases/<release_id>/history")
    @owner_api
    def get_release_history(release_id: str):
        verify = request.args.get("verify", "0") in ("1", "true", "yes")
        return ok(releases.release_history(release_id, verify=verify))

    def confirmed_republication(payload: dict) -> bool:
        if not isinstance(payload, dict):
            raise ValidationError("请求必须是 JSON 对象。")
        value = payload.get("confirm_republish_withdrawn", False)
        if type(value) is not bool:
            raise ValidationError("confirm_republish_withdrawn 必须是布尔值。", field="confirm_republish_withdrawn")
        return value

    @app.post("/api/releases/restore")
    @owner_api
    def restore_release():
        payload = request.get_json(silent=True) or {}
        confirmed = confirmed_republication(payload)
        content_ids = payload.get("content_ids")
        attachment_ids = payload.get("attachment_ids")
        if content_ids is not None and not isinstance(content_ids, list):
            raise ValidationError("content_ids 必须为数组。", field="content_ids")
        if attachment_ids is not None and not isinstance(
            attachment_ids, list
        ):
            raise ValidationError("attachment_ids 必须为数组。", field="attachment_ids")
        return ok(
            releases.restore(
                payload.get("source_release_id"),
                content_ids=content_ids,
                attachment_ids=attachment_ids,
                reason=payload.get("reason"),
                expected_active_release_id=payload.get(
                    "expected_active_release_id"
                ),
                confirm_republish_withdrawn=confirmed,
                dry_run=bool(payload.get("dry_run", False)),
            )
        )

    # ------------------------------------------------------------------
    # Owner-only read-only publication drift (P6-C)
    # ------------------------------------------------------------------
    @app.get("/api/publication/drift")
    @owner_api
    def publication_drift():
        # Read-only: comparing the active bundle with the private archive never
        # mutates the DB, registry, bundles, objects or audit log.
        return ok(releases.drift_report())

    def publish_context(plan):
        status = hosting.status(plan["active_release_id"], plan["content_ids"])
        confirmation = hashlib.sha256(json.dumps({
            "active": plan["active_release_id"],
            "target": status.get("target_id"),
            "contents": [(row["content_id"], row["revision"]) for row in plan["contents"]],
            "attachments": plan["attachment_ids"],
            "removals": status.get("pending_removal_ids"),
        }, sort_keys=True).encode()).hexdigest()
        return status, confirmation

    @app.get("/api/publish/plan")
    @owner_api
    def publish_plan():
        plan = releases.publish_plan()
        try:
            plan["deployment"], plan["confirmation"] = publish_context(plan)
        except PagesError as error:
            plan["deployment"] = {"status": "unavailable", "connection_error": error.message}
        return ok(plan)

    @app.post("/api/publish/preview-site")
    @owner_api
    def prepare_site_preview():
        nonlocal draft_site
        payload = request.get_json(silent=True) or {}
        plan = releases.publish_plan()
        if not plan["valid"]:
            raise ReleaseValidationError("当前选择无法生成预览。", issues=plan["issues"])
        if payload.get("confirmation") != publish_context(plan)[1]:
            raise ReleaseConflictError("待发布内容已变化，请刷新后重新预览。")
        directory = TemporaryDirectory(prefix="archive-site-preview-", ignore_cleanup_errors=True)
        root = Path(directory.name) / "site"
        try:
            manifest = releases.render_site_preview(root, plan)
        except Exception:
            directory.cleanup()
            raise
        if draft_site:
            draft_site["directory"].cleanup()
        nonce = secrets.token_hex(16)
        draft_site = {
            "nonce": nonce, "directory": directory, "root": root,
            "paths": {entry["path"] for entry in manifest["files"] if entry["kind"] != "payload"},
        }
        return ok({"url": url_for("draft_preview_file", nonce=nonce)})

    @app.post("/api/publish/cloudflare/projects")
    @owner_api
    def cloudflare_projects():
        payload = request.get_json(silent=True) or {}
        account, token = payload.get("account", ""), payload.get("token", "")
        if not isinstance(account, str) or not isinstance(token, str):
            raise PagesError("Cloudflare 配置格式不正确。")
        return ok({"projects": hosting.projects(account.strip(), token.strip())})

    @app.post("/api/publish/cloudflare/configure")
    @owner_api
    def configure_cloudflare():
        payload = request.get_json(silent=True) or {}
        values = [payload.get(key, "") for key in ("account", "project", "token")]
        if not all(isinstance(value, str) for value in values):
            raise PagesError("Cloudflare 配置格式不正确。")
        return ok(hosting.configure(*(value.strip() for value in values)))

    @app.post("/api/publish/cloudflare/check")
    @owner_api
    def check_cloudflare():
        hosting.check()
        plan = releases.publish_plan()
        return ok(hosting.status(plan["active_release_id"], plan["content_ids"]))

    @app.post("/api/publish/cloudflare/sync")
    @owner_api
    def sync_cloudflare():
        payload = request.get_json(silent=True) or {}
        active = releases.list()["active_release_id"]
        if payload.get("confirm") is not True or payload.get("expected_active") != active:
            raise ReleaseConflictError("请核对并确认当前本机网站。")
        return ok(hosting.deploy(active, payload.get("target_id")))

    @app.post("/api/publish/cloudflare/cleanup")
    @owner_api
    def cleanup_cloudflare():
        payload = request.get_json(silent=True) or {}
        active = releases.list()["active_release_id"]
        if payload.get("confirm") is not True or "expected_active" not in payload or payload["expected_active"] != active:
            raise ReleaseConflictError("请核对当前本机网站并单独批准该旧部署。")
        return ok(hosting.cleanup(active, payload.get("target_id"), payload.get("deployment_id"),
                                  payload.get("approval")))

    @app.post("/api/publish/one-click")
    @owner_api
    def publish_one_click():
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm") is not True:
            raise ReleaseValidationError("发布需要明确确认。", field="confirm")

        plan = releases.publish_plan()
        active = plan["active_release_id"]
        remove = payload.get("remove") is True
        online = payload.get("online") is True
        force = payload.get("force") is True
        deployment = {
            "status": "not_configured",
            "message": "线上部署尚未配置；本次操作只更新本机网站。",
        }
        withdrawal_successor = False
        if not online and payload.get("confirmation") is not None:
            if payload["confirmation"] != publish_context(plan)[1]:
                raise ReleaseConflictError("内容已变化，请刷新后重新确认。")
        if online:
            deployment, confirmation = publish_context(plan)
            if not deployment.get("target_id"):
                raise PagesError("请先连接 Cloudflare Pages。")
            if payload.get("confirmation") != confirmation:
                raise ReleaseConflictError("内容或托管项目已变化，请刷新后重新确认。")
            delta = plan["delta"]
            withdrawal_successor = (
                remove and not force and deployment.get("can_publish_withdrawal_successor")
                and (delta["removed_content_ids"] or delta["removed_attachment_ids"]
                     or active != deployment["last_attempt"]["release_id"])
                and not delta["added_content_ids"] and not delta["added_attachment_ids"]
            )
            if deployment["status"] == "unconfirmed" and not withdrawal_successor:
                raise PagesError("请先核对上一次部署的结果。")
            hosting.preflight_plan(plan)

        if remove:
            if payload.get("expected_active") != active:
                raise ReleaseConflictError("当前网站已变化，请刷新后重新确认。")
            if not (plan["delta"]["removed_content_ids"] or plan["delta"]["removed_attachment_ids"]
                    or withdrawal_successor or deployment.get("pending_removal_ids")
                    or deployment["status"] == "cleanup_pending"):
                raise ReleaseValidationError("当前网站没有待移除的内容。")

        built = False
        audit_pending = False
        previous = active
        if plan["empty"]:
            empty_online = online and deployment.get("last_attempt") is not None
            if not (remove or empty_online):
                raise ReleaseValidationError("没有已选择公开的内容，无法发布。", issues=plan["issues"])
            if active:
                releases.withdraw_all(reason="privacy-removal")
                active = None
        elif not plan["valid"]:
            raise ReleaseValidationError("发布前校验未通过。", issues=plan["issues"])
        elif (plan["delta"]["has_changes"] or not active or not releases.verify(active)["ok"]
              or (force and not online)):
            prepared = releases.build(plan["content_ids"], plan["attachment_ids"])
            try:
                activated = releases.activate(
                    prepared["release_id"], reason="one-click",
                    confirm_republish_withdrawn=payload.get("confirm_republish_withdrawn") is True,
                )
            except ReleaseConfirmationError as exc:
                return jsonify({
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": "该选择包含曾被撤回的内容，需要单独确认重新公开。",
                        "details": {"release_id": prepared["release_id"], "reason": exc.message},
                    },
                }), 409
            active = activated["release_id"]
            audit_pending = bool(activated.get("audit_event_pending"))
            built = True

        if online:
            deployment = hosting.deploy(active, deployment["target_id"], force=force,
                                        withdrawal_successor=bool(withdrawal_successor))
        return ok({
            "release_id": active,
            "active_release_id": active,
            "previous_active_release_id": previous if previous != active else None,
            "content_count": len(plan["content_ids"]),
            "attachment_count": len(plan["attachment_ids"]),
            "local_publish": "succeeded" if active else "withdrawn",
            "bundle_generated": built,
            "public_url": "/",
            "preview_url": "/preview/",
            "deployment": deployment,
            "audit_event_pending": audit_pending,
        })

    @app.get("/api/publication/state")
    @owner_api
    def publication_state():
        state = releases.publication_state()
        entries = state["contents"]
        try:
            deployment = hosting.status(
                state["release"]["active_release_id"],
                (row["content_id"] for row in entries if row.get("selected_for_publication")),
                withdrawal_required=state["release"]["withdrawal_required"],
            )
        except PagesError as error:
            deployment = {"status": "unavailable", "connection_error": error.message}
        state["deployment"] = deployment
        removal = set(deployment.get("pending_removal_ids", []))
        possible = set(deployment.get("online_content_ids", []))
        for row in entries:
            row["online_copy_possible"] = row["content_id"] in possible
            if row["content_id"] in removal or (row["state"] == "withdrawn" and row["online_copy_possible"]):
                row["online_removal_pending"] = True
                if row["state"] != "pending_removal":
                    state["counts"][row["state"]] -= 1
                    state["counts"]["pending_removal"] += 1
                    row.update(state="pending_removal", label="等待从公开网站移除")
        return ok(state)

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------
    @app.get("/api/public/content/<content_id>")
    def public_content(content_id: str):
        return ok(release_reader.content_payload(content_id))

    # ------------------------------------------------------------------
    # Owner activation / withdrawal / audit
    # ------------------------------------------------------------------
    @app.post("/api/releases/withdraw")
    @owner_api
    def withdraw_release():
        payload = request.get_json(silent=True) or {}
        target = payload.get("target_id")
        if target:
            if payload.get("confirm") is not True:
                raise ReleaseValidationError("线上撤回需要明确确认。")
            active = releases.list()["active_release_id"]
            remote = hosting.status(active)
            if payload.get("expected_active") != active or remote.get("target_id") != target:
                raise ReleaseConflictError("当前网站或托管项目已变化，请刷新后重新确认。")
            if remote["status"] == "unconfirmed" and not remote.get("can_publish_withdrawal_successor"):
                raise PagesError("请先核对上一次部署的结果。")
        result = releases.withdraw(
            payload.get("content_ids"),
            payload.get("attachment_ids"),
            reason=payload.get("reason"),
        )
        if target:
            result["deployment"] = hosting.deploy(result.get("active_release_id"), target,
                                                  withdrawal_successor=remote.get("can_publish_withdrawal_successor", False))
        return ok(result)

    @app.get("/api/releases/audit")
    @owner_api
    def audit_releases():
        return ok(releases.audit(write_report=True))

    @app.post("/api/releases/reconcile")
    @owner_api
    def reconcile_release_events():
        return ok(releases.reconcile_events())

    @app.post("/api/releases/<release_id>/activate")
    @owner_api
    def activate_release(release_id: str):
        payload = request.get_json(silent=True) or {}
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("reason 必须是文本。", field="reason")
        return ok(
            releases.activate(
                release_id,
                reason=reason,
                confirm_republish_withdrawn=confirmed_republication(payload),
            )
        )

    return app
