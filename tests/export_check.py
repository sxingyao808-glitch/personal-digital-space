"""Real-process export check against the actual launcher.

Starts run_app.py with the synthetic fixture, exports through HTTP, downloads
the package, and verifies the manifest and attachment identity. Output is JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from lifecycle_check import (  # noqa: E402 - same-directory verification helper
    TOKEN,
    Client,
    free_port,
    start_server,
    stop_server,
    wait_for_health,
)


def check() -> dict:
    report = {"ok": False, "steps": [], "errors": []}

    def step(name: str, ok: bool, detail: dict | None = None) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail or {}})
        if not ok:
            report["errors"].append(name)

    with tempfile.TemporaryDirectory() as folder:
        data_dir = Path(folder) / "data"
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        process = start_server(data_dir, port, init_fixture=True)
        try:
            step("server_health", wait_for_health(base), {"base": base})

            client = Client()
            status, _ = client.request("GET", f"{base}/auth?token={TOKEN}")
            step("owner_login", status == 200, {"status": status})

            status, session = client.json("GET", f"{base}/api/session")
            csrf = (session or {}).get("data", {}).get("csrf")
            step("session_csrf", status == 200 and bool(csrf), {"status": status})

            status, payload = client.json(
                "POST",
                f"{base}/api/export",
                body={},
                headers={"X-CSRF-Token": csrf},
            )
            data = (payload or {}).get("data", {})
            step(
                "export_created",
                status == 200 and data.get("status") == "complete",
                {
                    "status": status,
                    "export_status": data.get("status"),
                    "counts": data.get("counts"),
                    "issues": data.get("issues"),
                },
            )
            step(
                "fixture_counts",
                data.get("counts", {}).get("content_total") == 4
                and data.get("counts", {}).get("relations_total") == 2
                and data.get("counts", {}).get("attributes_total") == 2
                and data.get("counts", {}).get("attachments_included") == 2,
                {"counts": data.get("counts")},
            )

            status, blob = client.request("GET", f"{base}{data.get('download_path', '/api/export/none.zip')}")
            downloaded_sha = hashlib.sha256(blob).hexdigest()
            step(
                "download_matches_package",
                status == 200 and downloaded_sha == data.get("sha256"),
                {"status": status, "sha256": downloaded_sha, "bytes": len(blob)},
            )

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
                handle.write(blob)
                downloaded = Path(handle.name)
            try:
                with zipfile.ZipFile(downloaded) as archive:
                    names = set(archive.namelist())
                    required = {
                        "manifest.json",
                        "content.json",
                        "relations.json",
                        "attributes.json",
                        "attachments.json",
                        "integrity_issues.json",
                    }
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                    file_checks = []
                    for entry in manifest["files"]:
                        actual = archive.read(entry["path"])
                        file_checks.append(
                            len(actual) == entry["bytes"]
                            and hashlib.sha256(actual).hexdigest() == entry["sha256"]
                        )
                    attachments = json.loads(
                        archive.read("attachments.json").decode("utf-8")
                    )["items"]
                    attachment_checks = [
                        item["included"]
                        and hashlib.sha256(archive.read(item["export_path"])).hexdigest()
                        == item["exported_sha256"]
                        for item in attachments
                    ]
                    content_items = json.loads(
                        archive.read("content.json").decode("utf-8")
                    )["items"]
                    private_id = next(
                        item["id"] for item in content_items if not item["public"]
                    )
                    public_id = next(item["id"] for item in content_items if item["public"])
                    step(
                        "package_members_and_hashes",
                        required.issubset(names) and all(file_checks) and all(attachment_checks),
                        {
                            "members": sorted(names),
                            "file_hash_checks": file_checks,
                            "attachment_hash_checks": attachment_checks,
                        },
                    )
                    step(
                        "manifest_format",
                        manifest["format"] == "personal-archive-export"
                        and manifest["format_version"] == 1
                        and manifest["schema_version"] >= 1
                        and manifest["boundaries"]["publishes_content"] is False,
                        {
                            "format": manifest["format"],
                            "format_version": manifest["format_version"],
                            "schema_version": manifest["schema_version"],
                            "boundaries": manifest["boundaries"],
                        },
                    )
            finally:
                downloaded.unlink(missing_ok=True)

            anonymous = Client()
            private_page = anonymous.request("GET", f"{base}/p/{private_id}")[0]
            public_page = anonymous.request("GET", f"{base}/p/{public_id}")[0]
            step(
                "privacy_boundary_unchanged",
                private_page == 404 and public_page == 200,
                {"private_status": private_page, "public_status": public_page},
            )
            report["ok"] = not report["errors"]
        finally:
            stop_server(process)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-process export verification")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    report = check()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
