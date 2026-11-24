"""AC-16 cross-operation consistency matrix (real HTTP process).

Runs create -> modify -> delete -> restore -> read -> export against a running
local Archive server and checks, after every step, that stable identities,
relation endpoints/types, and attribute ownership/provenance/confirmed state are
preserved or truthfully reported. Failure scenarios are recorded separately.

Output is JSON so the raw result can be stored as evidence. This script does not
change any acceptance status by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import socket
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
TOKEN = "ac16-matrix-owner-token-8642097531"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method, url, **kwargs):
        status, payload = self.request(method, url, **kwargs)
        try:
            return status, json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return status, None


def multipart(fields: dict[str, str], filename: str, payload: bytes):
    boundary = f"----ac16{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode(
                "utf-8"
            )
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
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


def start_server(data_dir: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "run_app.py"),
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
            "--no-open",
            "--bootstrap-token",
            TOKEN,
        ],
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


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def check() -> dict:
    result: dict = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": [],
        "steps": [],
        "failures": [],
    }
    tempdir = tempfile.TemporaryDirectory()
    root = Path(tempdir.name)
    data_dir = root / "data"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    def record(name: str, expected, actual) -> bool:
        passed = expected == actual
        result["checks"].append(
            {"check": name, "expected": expected, "actual": actual, "pass": passed}
        )
        if not passed:
            result["failures"].append({"check": name, "expected": expected, "actual": actual})
        return passed

    def step(name: str, detail=None) -> None:
        result["steps"].append({"step": name, "detail": detail})

    process = start_server(data_dir, port)
    try:
        if not wait_for_health(base):
            raise AssertionError("server did not become healthy")
        owner = Client()
        status, _ = owner.request("GET", f"{base}/auth?token={TOKEN}")
        if status not in (200, 302):
            raise AssertionError(f"login failed: {status}")
        status, session = owner.json("GET", f"{base}/api/session")
        if status != 200 or not session or not session.get("ok"):
            raise AssertionError("owner session not established")
        headers = {"X-CSRF-Token": session["data"]["csrf"]}

        def create(title, body, visibility):
            status, payload = owner.json(
                "POST",
                f"{base}/api/content",
                body={"title": title, "body": body, "visibility": visibility},
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"create failed: {status} {payload}")
            return payload["data"]

        def upload(content_id, payload_bytes, filename):
            body, content_type = multipart(
                {"operation_id": f"op_{uuid.uuid4().hex}"}, filename, payload_bytes
            )
            status, payload = owner.json(
                "POST",
                f"{base}/api/content/{content_id}/attachments",
                raw=body,
                content_type=content_type,
                headers=headers,
            )
            if status != 201:
                raise AssertionError(f"upload failed: {status} {payload}")
            return payload["data"]["attachment"]

        def capture() -> dict:
            content = {}
            for content_id in (a["id"], b["id"], c["id"], d["id"]):
                status, payload = owner.json(
                    "GET", f"{base}/api/content/{content_id}?include_deleted=1"
                )
                if status == 200:
                    content[content_id] = payload["data"]
            relation_map: dict[str, dict] = {}
            for item in content.values():
                for row in item["relations"]:
                    relation_map[row["id"]] = {
                        "id": row["id"],
                        "from_id": row["from_id"],
                        "to_id": row["to_id"],
                        "type": row["type"],
                    }
            relations = sorted(relation_map.values(), key=lambda item: item["id"])
            attributes = sorted(
                [
                    {
                        "id": row["id"],
                        "content_id": row["content_id"],
                        "key": row["key"],
                        "value": row["value"],
                        "provenance": row["provenance"],
                        "confirmed": bool(row["confirmed"]),
                    }
                    for item in content.values()
                    for row in item["attributes"]
                ],
                key=lambda item: item["id"],
            )
            attachments = sorted(
                [
                    {
                        "id": row["id"],
                        "content_id": row["content_id"],
                        "state": row["state"],
                    }
                    for item in content.values()
                    for row in item["attachments"]
                ],
                key=lambda item: item["id"],
            )
            return {
                "content": {
                    key: {
                        "id": item["id"],
                        "title": item["title"],
                        "body": item["body"],
                        "revision": item["revision"],
                        "state": item["state"],
                        "visibility": item["visibility"],
                        "visibility_selected": item["visibility_selected"],
                        "deleted_at_present": item["deleted_at"] is not None,
                    }
                    for key, item in content.items()
                },
                "relations": relations,
                "attributes": attributes,
                "attachments": attachments,
            }

        def invariants(snapshot: dict) -> dict:
            return {
                "content_ids": sorted(snapshot["content"]),
                "relation_edges": snapshot["relations"],
                "attribute_states": [
                    {
                        "id": row["id"],
                        "content_id": row["content_id"],
                        "value": row["value"],
                        "provenance": row["provenance"],
                        "confirmed": row["confirmed"],
                    }
                    for row in snapshot["attributes"]
                ],
                "attachment_owners": [
                    {"id": row["id"], "content_id": row["content_id"]} for row in snapshot["attachments"]
                ],
            }

        # ------------------------------------------------------------
        # Create
        # ------------------------------------------------------------
        a = create("矩阵条目 A", "创建阶段的正文 A", "public")
        b = create("矩阵条目 B", "创建阶段的正文 B", "private")
        c = create("矩阵条目 C", "创建阶段的正文 C", "private")
        d = create("矩阵条目 D", "用于缺失资源失败场景", "private")
        attachment_a = upload(a["id"], b"matrix-attachment-a\n", "a.txt")
        attachment_c = upload(c["id"], b"matrix-attachment-c\n", "c.txt")
        step("create", {"a": a["id"], "b": b["id"], "c": c["id"], "d": d["id"]})

        status, relation_one = owner.json(
            "POST",
            f"{base}/api/content/{a['id']}/relations",
            body={"to_id": b["id"], "type": "REFERENCES"},
            headers=headers,
        )
        status_two, relation_two = owner.json(
            "POST",
            f"{base}/api/content/{b['id']}/relations",
            body={"to_id": c["id"], "type": "DERIVED_FROM"},
            headers=headers,
        )
        status_three, attribute_one = owner.json(
            "POST",
            f"{base}/api/content/{a['id']}/attributes",
            body={
                "key": "来源",
                "value": "本地记录",
                "provenance": "user",
                "confirmed": True,
            },
            headers=headers,
        )
        status_four, attribute_two = owner.json(
            "POST",
            f"{base}/api/content/{b['id']}/attributes",
            body={
                "key": "推测",
                "value": "AI 暂定",
                "provenance": "ai_suggested",
                "confirmed": False,
            },
            headers=headers,
        )
        record(
            "create_relation_status",
            [201, 201],
            [status, status_two],
        )
        record("create_attribute_status", [201, 201], [status_three, status_four])
        expected_edges = sorted(
            [
                {
                    "id": relation_one["data"]["id"],
                    "from_id": a["id"],
                    "to_id": b["id"],
                    "type": "REFERENCES",
                },
                {
                    "id": relation_two["data"]["id"],
                    "from_id": b["id"],
                    "to_id": c["id"],
                    "type": "DERIVED_FROM",
                },
            ],
            key=lambda item: item["id"],
        )
        expected_attributes = sorted(
            [
                {
                    "id": attribute_one["data"]["id"],
                    "content_id": a["id"],
                    "key": "来源",
                    "value": "本地记录",
                    "provenance": "user",
                    "confirmed": True,
                },
                {
                    "id": attribute_two["data"]["id"],
                    "content_id": b["id"],
                    "key": "推测",
                    "value": "AI 暂定",
                    "provenance": "ai_suggested",
                    "confirmed": False,
                },
            ],
            key=lambda item: item["id"],
        )
        s0 = capture()
        record("create_identity", True, a["id"].startswith("c_") and len({a["id"], b["id"], c["id"], d["id"]}) == 4)
        record("create_relation_edges", expected_edges, s0["relations"])
        record("create_attribute_states", expected_attributes, s0["attributes"])
        result["initial"] = invariants(s0)

        # ------------------------------------------------------------
        # Modify
        # ------------------------------------------------------------
        status, modified = owner.json(
            "PATCH",
            f"{base}/api/content/{a['id']}",
            body={
                "revision": s0["content"][a["id"]]["revision"],
                "title": "矩阵条目 A（已修改）",
                "body": "修改后的正文 A",
            },
            headers=headers,
        )
        if status != 200:
            raise AssertionError(f"modify failed: {status} {modified}")
        s1 = capture()
        record("modify_revision", s0["content"][a["id"]]["revision"] + 1, s1["content"][a["id"]]["revision"])
        record("modify_title", "矩阵条目 A（已修改）", s1["content"][a["id"]]["title"])
        record("modify_identity_stable", True, s1["content"][a["id"]]["id"] == a["id"])
        record("modify_relations_unchanged", s0["relations"], s1["relations"])
        record("modify_attributes_unchanged", s0["attributes"], s1["attributes"])
        record(
            "modify_other_records_unchanged",
            [s0["content"][b["id"]]["revision"], s0["content"][c["id"]]["revision"]],
            [s1["content"][b["id"]]["revision"], s1["content"][c["id"]]["revision"]],
        )
        step("modify", {"revision": s1["content"][a["id"]]["revision"]})

        # ------------------------------------------------------------
        # Failure: stale revision, duplicate relation, self relation
        # ------------------------------------------------------------
        status, stale = owner.json(
            "PATCH",
            f"{base}/api/content/{a['id']}",
            body={"revision": s0["content"][a["id"]]["revision"], "title": "不应写入"},
            headers=headers,
        )
        s2 = capture()
        record("stale_revision_status", 409, status)
        record("stale_revision_no_change", s1, s2)

        status, duplicate = owner.json(
            "POST",
            f"{base}/api/content/{a['id']}/relations",
            body={"to_id": b["id"], "type": "REFERENCES"},
            headers=headers,
        )
        s3 = capture()
        record("duplicate_relation_status", 409, status)
        record("duplicate_relation_no_change", s1, s3)

        status, self_relation = owner.json(
            "POST",
            f"{base}/api/content/{c['id']}/relations",
            body={"to_id": c["id"], "type": "REFERENCES"},
            headers=headers,
        )
        record("self_relation_status", 422, status)
        step("failure_scenarios", {"stale": 409, "duplicate": 409, "self": 422})

        # ------------------------------------------------------------
        # Delete
        # ------------------------------------------------------------
        status, deletion = owner.json(
            "DELETE",
            f"{base}/api/content/{b['id']}",
            body={"revision": s1["content"][b["id"]]["revision"]},
            headers=headers,
        )
        if status != 200:
            raise AssertionError(f"delete failed: {status} {deletion}")
        s4 = capture()
        record("delete_state", "deleted", s4["content"][b["id"]]["state"])
        record("delete_deleted_at", True, s4["content"][b["id"]]["deleted_at_present"])
        record("delete_relations_preserved", expected_edges, s4["relations"])
        record("delete_attributes_preserved", expected_attributes, s4["attributes"])
        record(
            "delete_other_records_unchanged",
            [s1["content"][a["id"]]["revision"], s1["content"][c["id"]]["revision"]],
            [s4["content"][a["id"]]["revision"], s4["content"][c["id"]]["revision"]],
        )
        status, missing = owner.json("GET", f"{base}/api/content/{b['id']}")
        status_deleted, deleted_detail = owner.json(
            "GET", f"{base}/api/content/{b['id']}?include_deleted=1"
        )
        record("delete_hidden_by_default", 404, status)
        record("delete_visible_with_flag", 200, status_deleted)
        status, edit_deleted = owner.json(
            "PATCH",
            f"{base}/api/content/{b['id']}",
            body={"revision": s4["content"][b["id"]]["revision"], "title": "不应写入"},
            headers=headers,
        )
        record("edit_deleted_status", 409, status)
        step("delete", {"impacts": deletion["data"]["impacts"]})

        # ------------------------------------------------------------
        # Restore
        # ------------------------------------------------------------
        status, restored = owner.json(
            "POST",
            f"{base}/api/content/{b['id']}/restore",
            body={},
            headers=headers,
        )
        if status != 200:
            raise AssertionError(f"restore failed: {status} {restored}")
        s5 = capture()
        record("restore_state", "active", s5["content"][b["id"]]["state"])
        record("restore_identity", b["id"], s5["content"][b["id"]]["id"])
        record("restore_deleted_at_cleared", False, s5["content"][b["id"]]["deleted_at_present"])
        record("restore_relations_preserved", expected_edges, s5["relations"])
        record("restore_attributes_preserved", expected_attributes, s5["attributes"])
        record(
            "restore_report",
            {"restored_identity": True, "relations": 2, "attributes": 1, "complete": True},
            {
                "restored_identity": restored["data"]["report"]["restored_identity"],
                "relations": restored["data"]["report"]["relations"],
                "attributes": restored["data"]["report"]["attributes"],
                "complete": restored["data"]["report"]["complete"],
            },
        )
        step("restore", restored["data"]["report"])

        # ------------------------------------------------------------
        # Read
        # ------------------------------------------------------------
        status_a, detail_a = owner.json("GET", f"{base}/api/content/{a['id']}")
        status_b, detail_b = owner.json("GET", f"{base}/api/content/{b['id']}")
        status_c, detail_c = owner.json("GET", f"{base}/api/content/{c['id']}")
        record("read_status", [200, 200, 200], [status_a, status_b, status_c])
        record(
            "read_titles",
            ["矩阵条目 A（已修改）", "矩阵条目 B", "矩阵条目 C"],
            [
                detail_a["data"]["title"],
                detail_b["data"]["title"],
                detail_c["data"]["title"],
            ],
        )
        read_relation_map: dict[str, dict] = {}
        for item in (detail_a["data"], detail_b["data"], detail_c["data"]):
            for row in item["relations"]:
                read_relation_map[row["id"]] = {
                    "id": row["id"],
                    "from_id": row["from_id"],
                    "to_id": row["to_id"],
                    "type": row["type"],
                }
        record(
            "read_relation_endpoints",
            expected_edges,
            sorted(read_relation_map.values(), key=lambda item: item["id"]),
        )
        record(
            "read_relation_direction",
            ["incoming", "incoming", "outgoing", "outgoing"],
            sorted(
                [
                    row["direction"]
                    for item in (detail_a["data"], detail_b["data"], detail_c["data"])
                    for row in item["relations"]
                ]
            ),
        )
        record(
            "read_attribute_states",
            expected_attributes,
            sorted(
                [
                    {
                        "id": row["id"],
                        "content_id": row["content_id"],
                        "key": row["key"],
                        "value": row["value"],
                        "provenance": row["provenance"],
                        "confirmed": bool(row["confirmed"]),
                    }
                    for item in (detail_a["data"], detail_b["data"], detail_c["data"])
                    for row in item["attributes"]
                ],
                key=lambda item: item["id"],
            ),
        )
        step("read", {"a": detail_a["data"]["title"], "b": detail_b["data"]["title"]})

        # ------------------------------------------------------------
        # Export (and verify the archive did not change)
        # ------------------------------------------------------------
        status, exported = owner.json("POST", f"{base}/api/export", body={}, headers=headers)
        if status != 200 or not exported["data"]["complete"]:
            raise AssertionError(f"export failed: {status} {exported}")
        download_path = exported["data"]["download_path"]
        status, package = owner.request("GET", f"{base}{download_path}")
        if status != 200:
            raise AssertionError(f"download failed: {status}")
        package_path = root / "matrix-export.zip"
        package_path.write_bytes(package)
        with zipfile.ZipFile(package_path) as archive:
            exported_content = json.loads(archive.read("content.json").decode("utf-8"))
            exported_relations = json.loads(archive.read("relations.json").decode("utf-8"))
            exported_attributes = json.loads(archive.read("attributes.json").decode("utf-8"))
            exported_attachments = json.loads(archive.read("attachments.json").decode("utf-8"))
            exported_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        s6 = capture()
        record("export_does_not_mutate", s5, s6)
        record(
            "export_content_ids",
            sorted([a["id"], b["id"], c["id"], d["id"]]),
            sorted(row["id"] for row in exported_content["items"]),
        )
        record(
            "export_relation_edges",
            sorted(
                [
                    {
                        "from_id": row["from_id"],
                        "to_id": row["to_id"],
                        "type": row["type"],
                    }
                    for row in expected_edges
                ],
                key=lambda item: (item["from_id"], item["to_id"], item["type"]),
            ),
            sorted(
                [
                    {
                        "from_id": row["from_id"],
                        "to_id": row["to_id"],
                        "type": row["type"],
                    }
                    for row in exported_relations["items"]
                ],
                key=lambda item: (item["from_id"], item["to_id"], item["type"]),
            ),
        )
        record(
            "export_relation_direction",
            ["from->to"],
            sorted({row["direction"] for row in exported_relations["items"]}),
        )
        record(
            "export_attribute_states",
            [
                {
                    "id": row["id"],
                    "content_id": row["content_id"],
                    "value": row["value"],
                    "provenance": row["provenance"],
                    "confirmed": row["confirmed"],
                }
                for row in expected_attributes
            ],
            [
                {
                    "id": row["id"],
                    "content_id": row["content_id"],
                    "value": row["value"],
                    "provenance": row["provenance"],
                    "confirmed": row["confirmed"],
                }
                for row in sorted(
                    exported_attributes["items"], key=lambda item: item["id"]
                )
            ],
        )
        record(
            "export_attachment_ownership",
            sorted([(attachment_a["id"], a["id"]), (attachment_c["id"], c["id"])]),
            sorted((row["id"], row["content_id"]) for row in exported_attachments["items"]),
        )
        record(
            "export_attachment_verified",
            True,
            all(
                row["included"] and row["verification"] == "match"
                for row in exported_attachments["items"]
            ),
        )
        record("export_manifest_complete", True, exported_manifest["completeness"]["complete"])
        step(
            "export",
            {
                "filename": exported["data"]["filename"],
                "bytes": exported["data"]["bytes"],
                "sha256": exported["data"]["sha256"],
            },
        )

        # ------------------------------------------------------------
        # Failure: missing attachment resource is identified, not hidden
        # ------------------------------------------------------------
        attachment_d = upload(d["id"], b"will-be-removed\n", "d.txt")
        (data_dir / "objects" / attachment_d["id"]).unlink()
        status, partial = owner.json("POST", f"{base}/api/export", body={}, headers=headers)
        partial_codes = {issue["code"] for issue in partial["data"]["issues"]}
        record("partial_export_status", 200, status)
        record("partial_export_complete", False, partial["data"]["complete"])
        record("partial_export_issue_identified", True, "attachment_file_missing" in partial_codes)
        s7 = capture()
        record(
            "partial_export_does_not_mutate",
            {
                "content_ids": s6["content"],
                "relations": s6["relations"],
                "attributes": s6["attributes"],
            },
            {
                "content_ids": s7["content"],
                "relations": s7["relations"],
                "attributes": s7["attributes"],
            },
        )
        step("missing_resource", {"codes": sorted(partial_codes)})

        result["ok"] = not result["failures"]
        return result
    except Exception as exc:  # noqa: BLE001 - reported as evidence
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False
        return result
    finally:
        stop_server(process)
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
