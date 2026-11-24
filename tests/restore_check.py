"""End-to-end backup and restore check against real processes (AC-13 evidence).

This script deliberately does not call the implementation's own audit helper.
It creates a real archive through HTTP, backs it up with the real CLI, restores
the actual artifact into a new directory, and then compares source and restored
databases, object bytes and served HTTP behavior independently.

Output is JSON so the result can be stored as evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "restore-check-owner-token-1357924680"
TABLES = ("content", "relations", "attributes", "attachments", "operations")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(self, method, url, *, body=None, headers=None, raw=None, content_type=None):
        data = raw
        request_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json(self, method, url, **kwargs):
        status, payload, headers = self.request(method, url, **kwargs)
        try:
            return status, json.loads(payload.decode("utf-8")), headers
        except json.JSONDecodeError:
            return status, None, headers


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes):
    boundary = f"----restorecheck{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode(
                "utf-8"
            )
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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


def start_server(data_dir: Path, port: int, init_fixture: bool = False) -> subprocess.Popen:
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
        command, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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


def dump_tables(data_dir: Path) -> dict[str, list[dict]]:
    conn = sqlite3.connect(str(data_dir / "archive.db"))
    conn.row_factory = sqlite3.Row
    try:
        return {
            table: [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in TABLES
        }
    finally:
        conn.close()


def run_cli(arguments: list[str], env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_app.py"), *arguments],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": completed.stdout[-2000:]}
    return {
        "command": ["python", "run_app.py", *arguments],
        "exit_code": completed.returncode,
        "result": payload,
        "stderr_tail": completed.stderr[-1200:] if completed.stderr else "",
    }


def login(client: Client, base: str):
    status, _, _ = client.request("GET", f"{base}/auth?token={TOKEN}")
    if status not in (200, 302):
        raise AssertionError(f"login failed with {status}")
    status, session, _ = client.json("GET", f"{base}/api/session")
    if status != 200 or not session or not session.get("ok"):
        raise AssertionError("owner session not established")
    return session["data"]["csrf"]


def check() -> dict:
    result: dict = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "steps": [],
        "failures": [],
    }
    tempdir = tempfile.TemporaryDirectory()
    root = Path(tempdir.name)
    source = root / "source"
    restored = root / "restored"
    backup_path = root / "archive-backup.zip"
    port_one = free_port()
    port_two = free_port()

    def step(name: str, detail=None, ok: bool = True):
        result["steps"].append({"step": name, "ok": ok, "detail": detail})
        if not ok:
            result["failures"].append({"step": name, "detail": detail})

    try:
        # ------------------------------------------------------------
        # 1. Create a real archive through HTTP
        # ------------------------------------------------------------
        process = start_server(source, port_one, init_fixture=True)
        try:
            base = f"http://127.0.0.1:{port_one}"
            if not wait_for_health(base):
                raise AssertionError("source server did not become healthy")
            owner = Client()
            csrf = login(owner, base)
            headers = {"X-CSRF-Token": csrf}

            status, created, _ = owner.json(
                "POST",
                f"{base}/api/content",
                body={
                    "title": "备份恢复验证",
                    "body": "该条目用于备份与恢复的端到端验证。",
                    "visibility": "public",
                },
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"content creation failed: {status}")
            content = created["data"]
            step("create_content", {"id": content["id"]})

            status, listing, _ = owner.json("GET", f"{base}/api/content")
            items = listing["data"]["items"]
            ids = {item["id"]: item["title"] for item in items}
            hello_id = next(key for key, title in ids.items() if title == "你好，世界")
            experiment_id = next(key for key, title in ids.items() if title == "一次本地实验")
            draft_id = next(key for key, title in ids.items() if title == "私密草稿")
            relation_source_id = next(key for key, title in ids.items() if title == "一段关系")

            status, relation, _ = owner.json(
                "POST",
                f"{base}/api/content/{content['id']}/relations",
                body={"to_id": experiment_id, "type": "REFERENCES"},
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"relation creation failed: {status}")
            step("add_relation", {"id": relation["data"]["id"]})

            status, attribute, _ = owner.json(
                "POST",
                f"{base}/api/content/{content['id']}/attributes",
                body={
                    "key": "验证来源",
                    "value": "本地",
                    "provenance": "user",
                    "confirmed": True,
                },
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"attribute creation failed: {status}")
            step("add_attribute", {"id": attribute["data"]["id"]})

            payload_one = b"backup-verification-attachment-one\n"
            body, content_type = multipart(
                {"operation_id": f"op_{uuid.uuid4().hex}"}, "file", "one.txt", payload_one
            )
            status, uploaded_one, _ = owner.json(
                "POST",
                f"{base}/api/content/{content['id']}/attachments",
                raw=body,
                content_type=content_type,
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"attachment one failed: {status}")
            attachment_one = uploaded_one["data"]["attachment"]["id"]

            payload_two = b"backup-verification-attachment-two\n"
            body, content_type = multipart(
                {"operation_id": f"op_{uuid.uuid4().hex}"}, "file", "two.txt", payload_two
            )
            status, uploaded_two, _ = owner.json(
                "POST",
                f"{base}/api/content/{experiment_id}/attachments",
                raw=body,
                content_type=content_type,
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"attachment two failed: {status}")
            attachment_two = uploaded_two["data"]["attachment"]["id"]
            step(
                "upload_attachments",
                {"one": attachment_one, "two": attachment_two},
            )

            status, deleted, _ = owner.json(
                "DELETE",
                f"{base}/api/content/{relation_source_id}",
                body={"revision": 1},
                headers=headers,
            )
            if status != 200:
                raise AssertionError(f"soft delete failed: {status}")
            step("soft_delete", {"id": relation_source_id})
        finally:
            stop_server(process)

        source_counts = {
            table: len(rows) for table, rows in dump_tables(source).items()
        }
        step("source_ready", source_counts)

        # ------------------------------------------------------------
        # 2. Real CLI backup
        # ------------------------------------------------------------
        backup_result = run_cli(
            [
                "--data-dir",
                str(source),
                "--backup",
                str(backup_path),
                "--report",
                str(root / "backup-report.json"),
            ]
        )
        if backup_result["exit_code"] != 0 or not backup_path.is_file():
            raise AssertionError(f"backup failed: {backup_result}")
        backup_payload = backup_result["result"]
        step(
            "backup",
            {
                "bytes": backup_payload["bytes"],
                "sha256": backup_payload["sha256"],
                "counts": backup_payload["counts"],
                "warnings": len(backup_payload["warnings"]),
            },
            backup_payload.get("complete") is True,
        )
        result["backup"] = {
            "path": str(backup_path),
            "bytes": backup_payload["bytes"],
            "sha256": backup_payload["sha256"],
            "counts": backup_payload["counts"],
            "warnings": backup_payload["warnings"],
            "generated_at": backup_payload["generated_at"],
        }

        # ------------------------------------------------------------
        # 3. Independent package verification
        # ------------------------------------------------------------
        with zipfile.ZipFile(backup_path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            names = set(archive.namelist())
            member_checks = []
            for entry in manifest["files"]:
                payload = archive.read(entry["path"])
                member_checks.append(
                    {
                        "path": entry["path"],
                        "bytes_match": len(payload) == entry["bytes"],
                        "sha256_match": sha256_bytes(payload) == entry["sha256"],
                    }
                )
        package_ok = all(
            check["bytes_match"] and check["sha256_match"] for check in member_checks
        ) and sha256_file(backup_path) == backup_payload["sha256"]
        step(
            "package_verification",
            {
                "members": len(member_checks),
                "schema_version": manifest["schema_version"],
                "format": manifest["format"],
                "publishes_content": manifest["boundaries"]["publishes_content"],
            },
            package_ok,
        )
        result["package"] = {
            "members": sorted(names),
            "member_checks": member_checks,
            "format": manifest["format"],
            "format_version": manifest["format_version"],
            "schema_version": manifest["schema_version"],
            "identity_counts": {
                key: len(value)
                for key, value in manifest["identity"].items()
                if isinstance(value, list)
            },
            "warnings": manifest["warnings"],
            "boundaries": manifest["boundaries"],
        }

        # ------------------------------------------------------------
        # 4. Real CLI restore into a new directory
        # ------------------------------------------------------------
        restore_result = run_cli(
            [
                "--restore",
                str(backup_path),
                "--target",
                str(restored),
                "--report",
                str(root / "restore-report.json"),
            ]
        )
        if restore_result["exit_code"] != 0 or not restored.is_dir():
            raise AssertionError(f"restore failed: {restore_result}")
        restore_payload = restore_result["result"]
        step(
            "restore",
            {
                "verified": restore_payload["verified"],
                "status": restore_payload["status"],
                "counts": restore_payload["counts"],
            },
            bool(restore_payload["verified"]),
        )
        result["restore"] = restore_payload

        # ------------------------------------------------------------
        # 5. Independent database + object comparison
        # ------------------------------------------------------------
        source_tables = dump_tables(source)
        restored_tables = dump_tables(restored)
        table_diffs = [
            table for table in TABLES if source_tables[table] != restored_tables[table]
        ]
        source_objects = {
            path.name: sha256_file(path)
            for path in (source / "objects").iterdir()
            if path.is_file()
        }
        restored_objects = {
            path.name: sha256_file(path)
            for path in (restored / "objects").iterdir()
            if path.is_file()
        }
        expected_objects = {
            row["id"] for row in source_tables["attachments"] if row["state"] == "READY"
        }
        objects_ok = restored_objects == {key: source_objects[key] for key in expected_objects}
        deleted_ids = [
            row["id"] for row in restored_tables["content"] if row["state"] == "deleted"
        ]
        audit = {
            "table_rows_equal": not table_diffs,
            "differing_tables": table_diffs,
            "objects_equal": objects_ok,
            "object_count": len(restored_objects),
            "deleted_content_ids": deleted_ids,
            "deleted_content_preserved": sorted(deleted_ids)
            == sorted(manifest["identity"]["deleted_content_ids"]),
            "relation_edges_match": [
                {
                    "id": row["id"],
                    "from_id": row["from_id"],
                    "to_id": row["to_id"],
                    "type": row["type"],
                }
                for row in restored_tables["relations"]
            ]
            == manifest["identity"]["relation_edges"],
            "attribute_states_match": [
                {
                    "id": row["id"],
                    "content_id": row["content_id"],
                    "provenance": row["provenance"],
                    "confirmed": bool(row["confirmed"]),
                }
                for row in restored_tables["attributes"]
            ]
            == manifest["identity"]["attribute_states"],
            "counts": {
                table: len(rows) for table, rows in restored_tables.items()
            },
        }
        independent_ok = (
            audit["table_rows_equal"]
            and audit["objects_equal"]
            and audit["deleted_content_preserved"]
            and audit["relation_edges_match"]
            and audit["attribute_states_match"]
        )
        step("independent_audit", audit, independent_ok)
        result["audit"] = audit
        if not independent_ok:
            raise AssertionError(f"independent audit mismatch: {audit}")

        # ------------------------------------------------------------
        # 6. Serve the restored archive and re-check behavior
        # ------------------------------------------------------------
        server_steps = []
        process = start_server(restored, port_two)
        try:
            base = f"http://127.0.0.1:{port_two}"
            if not wait_for_health(base):
                raise AssertionError("restored server did not become healthy")
            owner = Client()
            csrf = login(owner, base)
            headers = {"X-CSRF-Token": csrf}

            status, listing, _ = owner.json("GET", f"{base}/api/content")
            listed_ids = {item["id"] for item in listing["data"]["items"]}
            active_ids = {
                row["id"] for row in restored_tables["content"] if row["state"] == "active"
            }
            server_steps.append(
                {
                    "step": "list_content",
                    "ok": listed_ids == active_ids,
                    "count": len(listed_ids),
                }
            )

            status, trash, _ = owner.json("GET", f"{base}/api/content?deleted=1")
            trash_ids = {item["id"] for item in trash["data"]["items"]}
            server_steps.append(
                {
                    "step": "deleted_visible",
                    "ok": set(deleted_ids).issubset(trash_ids),
                    "deleted_count": len(set(deleted_ids) & trash_ids),
                }
            )

            status, detail, _ = owner.json("GET", f"{base}/api/content/{content['id']}")
            attributes = detail["data"]["attributes"]
            relations = detail["data"]["relations"]
            server_steps.append(
                {
                    "step": "read_detail",
                    "ok": status == 200 and bool(attributes) and bool(relations),
                    "attribute_provenance": attributes[0]["provenance"] if attributes else None,
                    "relation_type": relations[0]["type"] if relations else None,
                }
            )

            status, payload, _ = owner.request("GET", f"{base}/api/attachments/{attachment_one}/file")
            server_steps.append(
                {
                    "step": "download_attachment",
                    "ok": status == 200 and sha256_bytes(payload) == sha256_bytes(payload_one),
                    "sha256": sha256_bytes(payload) if status == 200 else None,
                }
            )

            anonymous = Client()
            status, page, _ = anonymous.request("GET", f"{base}/p/{hello_id}")
            server_steps.append(
                {
                    "step": "public_page",
                    "ok": status == 200 and "你好，世界" in page.decode("utf-8", "replace"),
                }
            )
            status, _, _ = anonymous.request("GET", f"{base}/p/{draft_id}")
            server_steps.append({"step": "private_page_404", "ok": status == 404})
            status, _, _ = anonymous.request("GET", f"{base}/p/{relation_source_id}")
            server_steps.append({"step": "deleted_page_404", "ok": status == 404})

            all_ok = all(entry["ok"] for entry in server_steps)
            step("restored_server", server_steps, all_ok)
            result["restored_server"] = server_steps
            if not all_ok:
                raise AssertionError(f"restored server checks failed: {server_steps}")
        finally:
            stop_server(process)

        # ------------------------------------------------------------
        # 7. Failure semantics: existing target, corrupted package,
        #    missing member, interrupted backup/restore
        # ------------------------------------------------------------
        negatives = []

        existing_target = root / "existing-target"
        existing_target.mkdir()
        (existing_target / "sentinel.txt").write_text("keep-me", encoding="utf-8")
        existing = run_cli(["--restore", str(backup_path), "--target", str(existing_target)])
        negatives.append(
            {
                "case": "target_exists",
                "exit_code": existing["exit_code"],
                "error_code": (existing["result"] or {}).get("code"),
                "sentinel_intact": (existing_target / "sentinel.txt").read_text(encoding="utf-8")
                == "keep-me",
                "ok": existing["exit_code"] == 1
                and (existing["result"] or {}).get("code") == "validation_error",
            }
        )

        tampered = root / "tampered.zip"
        with zipfile.ZipFile(backup_path) as original, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in original.infolist():
                payload = original.read(info.filename)
                if info.filename == "archive.db":
                    payload = payload[:-32] + b"\x00" * 32
                updated.writestr(info.filename, payload)
        corrupted_target = root / "corrupted-target"
        corrupted = run_cli(["--restore", str(tampered), "--target", str(corrupted_target)])
        negatives.append(
            {
                "case": "corrupted_package",
                "exit_code": corrupted["exit_code"],
                "error_code": (corrupted["result"] or {}).get("code"),
                "target_absent": not corrupted_target.exists(),
                "ok": corrupted["exit_code"] == 1
                and (corrupted["result"] or {}).get("code") == "restore_failed"
                and not corrupted_target.exists(),
            }
        )

        missing_member = root / "missing-member.zip"
        with zipfile.ZipFile(backup_path) as original, zipfile.ZipFile(
            missing_member, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in original.infolist():
                if info.filename == f"objects/{attachment_one}":
                    continue
                updated.writestr(info.filename, original.read(info.filename))
        missing_target = root / "missing-target"
        missing = run_cli(
            ["--restore", str(missing_member), "--target", str(missing_target)]
        )
        negatives.append(
            {
                "case": "missing_attachment_member",
                "exit_code": missing["exit_code"],
                "error_code": (missing["result"] or {}).get("code"),
                "target_absent": not missing_target.exists(),
                "ok": missing["exit_code"] == 1 and not missing_target.exists(),
            }
        )

        interrupted_target = root / "interrupted-backup.zip"
        interrupted_backup = run_cli(
            [
                "--data-dir",
                str(source),
                "--backup",
                str(interrupted_target),
            ],
            env_extra={
                "ARCHIVE_TEST_MODE": "1",
                "ARCHIVE_FAULT_INJECT": "backup_after_snapshot",
            },
        )
        negatives.append(
            {
                "case": "interrupted_backup",
                "exit_code": interrupted_backup["exit_code"],
                "package_absent": not interrupted_target.exists(),
                "ok": interrupted_backup["exit_code"] == 74
                and not interrupted_target.exists(),
            }
        )

        interrupted_restore_target = root / "interrupted-restore"
        interrupted_restore = run_cli(
            ["--restore", str(backup_path), "--target", str(interrupted_restore_target)],
            env_extra={
                "ARCHIVE_TEST_MODE": "1",
                "ARCHIVE_FAULT_INJECT": "restore_after_extract",
            },
        )
        source_after = dump_tables(source)
        negatives.append(
            {
                "case": "interrupted_restore",
                "exit_code": interrupted_restore["exit_code"],
                "target_absent": not interrupted_restore_target.exists(),
                "source_unchanged": source_after == source_tables,
                "stale_staging": [
                    path.name
                    for path in root.glob(".interrupted-restore.restore-*")
                ],
                "ok": interrupted_restore["exit_code"] == 74
                and not interrupted_restore_target.exists()
                and source_after == source_tables,
            }
        )

        step("failure_semantics", negatives, all(entry["ok"] for entry in negatives))
        result["failure_semantics"] = negatives
        if not all(entry["ok"] for entry in negatives):
            raise AssertionError(f"failure semantics mismatch: {negatives}")

        # ------------------------------------------------------------
        # 8. Source archive is untouched by backup and restore
        # ------------------------------------------------------------
        source_final = dump_tables(source)
        source_untouched = source_final == source_tables
        step("source_untouched", {"unchanged": source_untouched}, source_untouched)
        if not source_untouched:
            raise AssertionError("source archive changed during backup/restore")

        result["ok"] = not result["failures"]
        return result
    except Exception as exc:  # noqa: BLE001 - reported as evidence
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False
        return result
    finally:
        tempdir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = check()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
