"""Local launcher for the Archive service.

Examples:
    python run_app.py --data-dir ./.archive-data
    python run_app.py --data-dir ./.archive-data --port 8765 --no-open
    python run_app.py --data-dir ./.archive-data --backup
    python run_app.py --data-dir ./.archive-data --registry-backup
    python run_app.py --restore backup.zip --target ./restored
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from flask import Flask, jsonify, request

from archive import __version__
from archive.app import create_app
from archive.attachments import AttachmentStore, _hash_file
from archive.auth import OwnerSecretError
from archive.backup import ArchiveBackup, ArchiveRestorer
from archive.db import SCHEMA_VERSION, SchemaVersionError, connect, get_meta, init_db, set_meta
from archive.win_data import diagnose_existing_permissions, make_data_dir
from archive.instance_lock import DataDirLock, DataDirLockedError
from archive.release import ReleaseRegistry
from archive.store import ArchiveError, ArchiveStore

# Desktop shell protocol. The shell parses exactly this prefix on stdout after
# the loopback server is listening; the line never contains the bootstrap token
# because the shell generated it and passed it in.
DESKTOP_READY_PREFIX = "ARCHIVE_DESKTOP_READY "
# Exit code used only in desktop mode: the selected directory is not an archive
# data directory. It is deliberately distinct from lock (3) and schema (2).
EXIT_UNEXPECTED_DATA_DIR = 4


def _load_fixture(data_dir: Path, store: ArchiveStore, attachments: AttachmentStore) -> None:
    """Create a small synthetic dataset in a disposable/demo data directory."""
    hello = store.create_content(
        "你好，世界",
        "这是我的第一个 Archive 条目。它完全保存在本机。",
        visibility="public",
    )
    experiment = store.create_content(
        "一次本地实验",
        "这里记录了关于稳定身份与附件恢复的设计实验。",
        visibility="public",
    )
    private_draft = store.create_content(
        "私密草稿",
        "第三方不可见的内容，即便通过猜测地址也无法访问。",
        visibility="private",
    )
    relation_source = store.create_content(
        "一段关系",
        "演示 PART_OF 关系如何引用其它条目，而不是复制内容。",
        visibility="private",
    )

    store.add_relation(relation_source["id"], experiment["id"], "PART_OF")
    store.add_relation(experiment["id"], hello["id"], "DERIVED_FROM")
    store.add_attribute(experiment["id"], "灵感来源", "本地验证", "user", confirmed=True)
    store.add_attribute(
        experiment["id"], "可能的意义", "承载关系结构", "ai_suggested", confirmed=False
    )

    sample = data_dir / "fixture_sample.txt"
    sample.write_bytes(b"Sample attachment bytes for the local Archive fixture.\n")
    digest, size = _hash_file(sample)
    operation_id = f"op_{secrets.token_hex(12)}"
    try:
        with sample.open("rb") as handle:
            result = attachments.upload(
                handle,
                filename="fixture_sample.txt",
                content_id=experiment["id"],
                operation_id=operation_id,
                declared_mime="text/plain",
                expected_sha256=digest,
                expected_size=size,
            )
        attachments.set_visibility(result["attachment"]["id"], "public")
    finally:
        sample.unlink(missing_ok=True)

    # Keep a private attachment too, so the public/private boundary is visible.
    private_bytes = b"Private attachment bytes. Must not appear publicly.\n"
    sample.write_bytes(private_bytes)
    private_digest, private_size = _hash_file(sample)
    private_op = f"op_{secrets.token_hex(12)}"
    try:
        with sample.open("rb") as handle:
            attachments.upload(
                handle,
                filename="private_note.txt",
                content_id=private_draft["id"],
                operation_id=private_op,
                declared_mime="text/plain",
                expected_sha256=private_digest,
                expected_size=private_size,
            )
    finally:
        sample.unlink(missing_ok=True)

    set_meta(data_dir / "archive.db", "fixture_loaded", "1")


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _emit(payload: dict, report: Path | None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    if report is not None:
        _write_report(report, payload)


def _schema_error_text(exc: SchemaVersionError) -> str:
    """Add only the diagnostic fields that are actually present."""
    parts = [exc.message]
    details = exc.details or {}
    if "reason" in details:
        parts.append(f"原因={details['reason']}")
    if "database_version" in details:
        parts.append(f"数据库版本={details['database_version']}")
    if "supported_version" in details:
        parts.append(f"当前支持版本={details['supported_version']}")
    if "target_version" in details:
        parts.append(f"目标版本={details['target_version']}")
    if "missing_versions" in details:
        parts.append(f"缺失迁移={details['missing_versions']}")
    if "failed_version" in details:
        parts.append(f"失败迁移={details['failed_version']}")
    return "；".join(parts)


def unexpected_data_dir_reason(data_dir: Path) -> str | None:
    """Refuse a directory that exists, has user files, but holds no archive.db.

    Initialising an empty database there would look like data loss, so the
    desktop entry point stops instead of guessing. ``.app.lock`` is ignored: it
    is a lock anchor created by this program and carries no content.
    """
    if not data_dir.exists():
        return None
    if (data_dir / "archive.db").exists():
        return None
    try:
        entries = [entry for entry in data_dir.iterdir() if entry.name != ".app.lock"]
    except OSError as exc:
        return f"数据目录无法读取：{type(exc).__name__}"
    if not entries:
        return None
    return "数据目录非空但缺少 archive.db"


def register_desktop_control(
    app: Flask, control_token: str, stop: Callable[[], None]
) -> None:
    """Expose a token-gated "stop this server" route for the desktop shell.

    The route only asks the process to stop its own WSGI loop; it is not a
    general command surface and it is registered only when the launcher supplied
    a control token.
    """

    @app.post("/api/desktop/shutdown")
    def desktop_shutdown():
        supplied = request.headers.get("X-Desktop-Control", "")
        if not hmac.compare_digest(supplied, control_token):
            return jsonify(
                {
                    "ok": False,
                    "error": {"code": "control_denied", "message": "控制令牌无效。"},
                }
            ), 403
        threading.Thread(target=stop, name="desktop-shutdown", daemon=True).start()
        return jsonify({"ok": True, "data": {"stopping": True}}), 200


def _stdin_pipe_handle() -> int | None:
    """Return the OS handle of stdin when it is a pipe, otherwise None.

    The watchdog only makes sense when a parent process owns the other end of a
    pipe. A console handle or a missing stdin disables it instead of guessing.
    """
    if sys.platform != "win32":
        return None
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    if handle == -1:
        return None
    import ctypes

    # FILE_TYPE_PIPE == 3
    if ctypes.windll.kernel32.GetFileType(ctypes.c_void_p(handle)) != 3:
        return None
    return handle


def _pipe_writer_gone(handle: int) -> bool:
    """Non-blocking check for a broken pipe (the parent process is gone)."""
    import ctypes

    available = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.PeekNamedPipe(
        ctypes.c_void_p(handle), None, 0, None, ctypes.byref(available), None
    )
    return not ok


def watch_parent_pipe(stop: Callable[[], None], finished: threading.Event) -> None:
    """Stop the service when the desktop shell that spawned it disappears.

    The shell keeps our stdin pipe open for the lifetime of the app. Once that
    pipe breaks the shell is gone, including after a force kill, and the service
    must not keep holding the data-directory lock. Polling avoids a blocking
    read on the buffered stdin object, which would abort the interpreter during
    shutdown. Only started when the launcher explicitly asks for it, so ordinary
    CLI use and tests never depend on stdin.
    """
    handle = _stdin_pipe_handle()
    if handle is None:
        return
    while not finished.wait(0.5):
        if _pipe_writer_gone(handle):
            print("\n桌面外壳已退出，正在停止本地服务。", flush=True)
            stop()
            return


def runtime_info() -> dict:
    """Probe loaded SQLite and actual archive settings using a disposable database."""
    from contextlib import closing
    import ctypes
    import platform
    import tempfile

    with tempfile.TemporaryDirectory(prefix="archive-runtime-") as temporary:
        path = Path(temporary) / "archive.db"
        init_db(path)
        with closing(connect(path)) as conn:
            version, source_id = conn.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
            pragmas = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                       for name in ("journal_mode", "synchronous", "foreign_keys", "busy_timeout", "wal_autocheckpoint", "page_size")}
    library = None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_module = kernel.GetModuleHandleW
        get_module.argtypes = (ctypes.c_wchar_p,)
        get_module.restype = ctypes.c_void_p
        get_path = kernel.GetModuleFileNameW
        get_path.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32)
        get_path.restype = ctypes.c_uint32
        dll = get_module("sqlite3.dll")
        buffer = ctypes.create_unicode_buffer(32768)
        if dll and 0 < get_path(dll, buffer, len(buffer)) < len(buffer):
            library = buffer.value
    return {"python_version": platform.python_version(), "sqlite_version": version,
            "sqlite_source_id": source_id, "sqlite_loaded_path": library, "pragmas": pragmas}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive local service")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--bootstrap-token", default=None)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="桌面外壳模式：不打开浏览器、不回显令牌、就绪后输出机器可读握手。",
    )
    parser.add_argument(
        "--control-token",
        default=None,
        metavar="TOKEN",
        help="桌面外壳持有的控制令牌；提供后注册受控停止接口。仅可与 --desktop 一起使用。",
    )
    parser.add_argument(
        "--watch-parent",
        action="store_true",
        help="桌面外壳退出（stdin 管道关闭）时停止服务。仅可与 --desktop 一起使用。",
    )
    parser.add_argument("--init-fixture", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument(
        "--backup",
        nargs="?",
        const="",
        default=None,
        metavar="ZIP",
        help="创建完整归档备份后退出；省略路径时写入 <data-dir>/backups/。",
    )
    parser.add_argument(
        "--registry-backup",
        nargs="?",
        const="",
        default=None,
        metavar="ZIP",
        help="备份发布注册表与 Release Bundle 后退出；省略路径时写入 <data-dir>/release-backups/。",
    )
    parser.add_argument(
        "--restore",
        dest="restore_from",
        default=None,
        metavar="ZIP",
        help="从备份包恢复到独立目标目录后退出；必须同时提供 --target。",
    )
    parser.add_argument("--restore-registry", type=Path, metavar="ZIP",
                        help="将注册表备份恢复到已恢复且停用的归档目录。")
    parser.add_argument("--target", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--runtime-info", action="store_true", help="在临时数据库中核对实际运行时。")
    args = parser.parse_args(argv)

    # Desktop and source clients consume CLI output as UTF-8. Embedded and
    # frozen Windows Python may ignore PYTHONUTF8 and PYTHONIOENCODING.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    selected_actions = [
        name
        for name, value in (
            ("--runtime-info", args.runtime_info or None),
            ("--backup", args.backup),
            ("--restore", args.restore_from),
            ("--restore-registry", args.restore_registry),
            ("--registry-backup", args.registry_backup),
        )
        if value is not None
    ]
    if len(selected_actions) > 1:
        parser.error(f"{'、'.join(selected_actions)} 不能同时使用。")

    if args.control_token is not None and not args.desktop:
        parser.error("--control-token 只能与 --desktop 一起使用。")

    if args.watch_parent and not args.desktop:
        parser.error("--watch-parent 只能与 --desktop 一起使用。")

    if args.desktop and not args.bootstrap_token:
        # The shell owns the token and builds the login URL from it. Generating
        # a hidden token here would leave the desktop window unable to sign in.
        parser.error("--desktop 需要 --bootstrap-token。")

    if args.runtime_info:
        _emit(runtime_info(), args.report)
        return 0

    if args.restore_registry is not None:
        if args.data_dir is None:
            parser.error("--restore-registry 需要明确指定 --data-dir。")
        try:
            result = ReleaseRegistry(args.data_dir).restore_backup(args.restore_registry)
        except (ArchiveError, OSError, ValueError, KeyError, TypeError) as exc:
            _emit({"ok": False, "code": "registry_restore_failed", "error": str(exc)}, args.report)
            return 1
        _emit({"ok": True, **result}, args.report)
        return 0

    if args.restore_from is not None:
        if args.target is None:
            parser.error("--restore 需要 --target。")
        try:
            result = ArchiveRestorer(args.restore_from).restore(args.target)
        except ArchiveError as exc:
            _emit(
                {
                    "ok": False,
                    "code": exc.code,
                    "error": exc.message,
                    "details": exc.details,
                },
                args.report,
            )
            return 1
        _emit({"ok": True, **result}, args.report)
        return 0

    if args.backup is not None:
        data_dir = (args.data_dir or (Path.cwd() / "archive_data")).resolve()
        try:
            result = ArchiveBackup(data_dir).create(args.backup or None)
        except ArchiveError as exc:
            _emit(
                {
                    "ok": False,
                    "code": exc.code,
                    "error": exc.message,
                    "details": exc.details,
                },
                args.report,
            )
            return 1
        _emit({"ok": True, **result}, args.report)
        return 0

    if args.registry_backup is not None:
        data_dir = (args.data_dir or (Path.cwd() / "archive_data")).resolve()
        try:
            with DataDirLock(data_dir):
                result = ReleaseRegistry(data_dir).backup(args.registry_backup or None)
        except ArchiveError as exc:
            _emit(
                {
                    "ok": False,
                    "kind": "release_registry",
                    "code": exc.code,
                    "error": exc.message,
                    "details": exc.details,
                },
                args.report,
            )
            return 1
        _emit(
            {
                "ok": True,
                "kind": "release_registry",
                "notice": (
                    "此备份只包含发布注册表与 Release Bundle，不含归档数据库、附件对象与 owner_secret；"
                    "它不能替代 --backup，也不能用作完整数据目录恢复。"
                ),
                **result,
            },
            args.report,
        )
        return 0

    data_dir = (args.data_dir or (Path.cwd() / "archive_data")).resolve()
    if args.desktop:
        reason = unexpected_data_dir_reason(data_dir)
        if reason is not None:
            print(f"数据目录不可用，已停止：{reason}。", file=sys.stderr, flush=True)
            print(f"数据目录: {data_dir}", file=sys.stderr, flush=True)
            print(
                "桌面版不会在可疑目录中新建空数据库。请检查数据目录设置。",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_UNEXPECTED_DATA_DIR
    existed = data_dir.is_dir()
    try:
        make_data_dir(data_dir)
    except OSError:
        print("无法创建私有数据目录。请检查路径和权限。", file=sys.stderr, flush=True)
        return EXIT_UNEXPECTED_DATA_DIR
    try:
        lock = DataDirLock(data_dir).acquire()
    except DataDirLockedError as exc:
        print(
            "数据目录已被另一个正在运行的 Archive 实例占用，已停止。",
            file=sys.stderr,
            flush=True,
        )
        print(f"数据目录: {data_dir}", file=sys.stderr, flush=True)
        print(f"占用进程 PID: {exc.details.get('holder_pid')}", file=sys.stderr, flush=True)
        print(
            "如确认本机没有其它实例在运行，请先正常退出该实例；不要手工删除 .app.lock。",
            file=sys.stderr,
            flush=True,
        )
        _emit(
            {
                "ok": False,
                "code": exc.code,
                "error": exc.message,
                "details": exc.details,
            },
            args.report,
        )
        return 3
    try:
        if existed and args.desktop:
            diagnose_existing_permissions(data_dir)
        return _serve(args, data_dir)
    finally:
        lock.release()


def _serve(args: argparse.Namespace, data_dir: Path) -> int:
    try:
        schema_report = init_db(data_dir / "archive.db")
    except SchemaVersionError as exc:
        print(f"数据库 schema 不兼容，已停止：{_schema_error_text(exc)}", file=sys.stderr, flush=True)
        return 2
    if schema_report["created"]:
        print("已初始化数据库 schema v1。", flush=True)
    elif schema_report["migrated"]:
        print(
            "数据库 schema 已迁移：v{} -> v{}（应用 {}）。".format(
                schema_report["from_version"],
                schema_report["to_version"],
                schema_report["applied_versions"],
            ),
            flush=True,
        )

    store = ArchiveStore(data_dir)
    attachments = AttachmentStore(data_dir)

    if args.init_fixture and get_meta(data_dir / "archive.db", "fixture_loaded") is None:
        _load_fixture(data_dir, store, attachments)

    if args.reconcile:
        report = attachments.reconcile()
        print(
            "Startup reconciliation: ok={} issues={}".format(
                report["ok"], report["counts"]["issues"]
            ),
            flush=True,
        )

    bootstrap_token = args.bootstrap_token or secrets.token_urlsafe(24)
    try:
        app = create_app(data_dir, bootstrap_token=bootstrap_token)
    except OwnerSecretError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 5
    except SchemaVersionError as exc:
        print(f"数据库 schema 不兼容，已停止：{_schema_error_text(exc)}", file=sys.stderr, flush=True)
        return 2

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", args.port, app, threaded=True)
    actual_port = server.server_port
    if args.control_token:
        register_desktop_control(app, args.control_token, server.shutdown)
    parent_watch_done = threading.Event()
    if args.watch_parent:
        threading.Thread(
            target=watch_parent_pipe,
            args=(server.shutdown, parent_watch_done),
            name="parent-pipe",
            daemon=True,
        ).start()
    login_url = f"http://127.0.0.1:{actual_port}/auth?token={bootstrap_token}"

    if args.desktop:
        # Machine-readable readiness line: the shell waits for it instead of
        # scraping human text or guessing a delay. No token appears here.
        handshake = {
            "version": __version__,
            "port": actual_port,
            "pid": os.getpid(),
            "data_dir": str(data_dir),
            "schema_version": SCHEMA_VERSION,
        }
        print(DESKTOP_READY_PREFIX + json.dumps(handshake, ensure_ascii=True), flush=True)
        print("\n" + "=" * 64, flush=True)
        print("Archive 已启动（桌面模式，仅监听 127.0.0.1）。", flush=True)
        print("地址:", f"http://127.0.0.1:{actual_port}/", flush=True)
        print("数据目录:", data_dir, flush=True)
        print("令牌由桌面外壳持有，不回显。", flush=True)
        print("单实例锁:", data_dir / ".app.lock", flush=True)
        print("=" * 64 + "\n", flush=True)
    else:
        print("\n" + "=" * 64, flush=True)
        print("Archive 已启动（本地私有，仅监听 127.0.0.1）。", flush=True)
        print("地址:", f"http://127.0.0.1:{actual_port}/", flush=True)
        print("所有者令牌:", bootstrap_token, flush=True)
        print("一键登录:", login_url, flush=True)
        print("数据目录:", data_dir, flush=True)
        print("单实例锁:", data_dir / ".app.lock", flush=True)
        print("=" * 64 + "\n", flush=True)
        if not args.no_open:
            webbrowser.open(login_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArchive 已停止。", flush=True)
    finally:
        parent_watch_done.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
