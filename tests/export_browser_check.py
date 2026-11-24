"""Browser verification of the owner export entry point.

Starts the real launcher with the fixture, logs in through Edge, clicks the
export button, and verifies the downloaded package and UI feedback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from lifecycle_check import (  # noqa: E402 - same-directory verification helper
    TOKEN,
    free_port,
    start_server,
    stop_server,
    wait_for_health,
)


def run(base_url: str, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "ok": False,
        "base_url": base_url,
        "steps": [],
        "console": [],
        "page_errors": [],
        "request_failures": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 960}, accept_downloads=True
        )
        page = context.new_page()
        page.on(
            "console",
            lambda message: result["console"].append(
                {"type": message.type, "text": message.text}
            )
            if message.type in ("error", "warning")
            else None,
        )
        page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
        page.on(
            "requestfailed",
            lambda request: result["request_failures"].append(
                {"url": request.url, "failure": request.failure}
            ),
        )

        page.goto(f"{base_url}/auth?token={TOKEN}", wait_until="networkidle")
        page.wait_for_selector("#content-list .content-item")
        expect(page.locator("#export-button")).to_have_text("导出归档")
        result["steps"].append({"owner_page_loaded": "pass"})

        with page.expect_download(timeout=20000) as download_info:
            page.click("#export-button")
        download = download_info.value
        target = output_dir / "downloaded-export.zip"
        download.save_as(target)
        expect(page.locator("#toast")).to_contain_text("导出完成")
        result["steps"].append(
            {
                "export_button": "pass",
                "suggested_filename": download.suggested_filename,
                "toast": page.locator("#toast").text_content(),
            }
        )

        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            attachments = json.loads(archive.read("attachments.json").decode("utf-8"))["items"]
            result["steps"].append(
                {
                    "package": "pass",
                    "complete": manifest["completeness"]["complete"],
                    "counts": manifest["counts"],
                    "attachment_files": sum(1 for a in attachments if a["included"]),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )

        page.screenshot(path=str(output_dir / "export-ui.png"), full_page=True)
        browser.close()

    result["ok"] = (
        not result["page_errors"]
        and not result["request_failures"]
        and not [c for c in result["console"] if c["type"] == "error"]
        and all(step.get("package") != "fail" for step in result["steps"])
        and any(step.get("complete") is True for step in result["steps"])
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser export verification")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        data_dir = Path(folder) / "data"
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        process = start_server(data_dir, port, init_fixture=True)
        try:
            if not wait_for_health(base):
                report = {"ok": False, "errors": ["server did not become healthy"]}
            else:
                report = run(base, args.output_dir)
        finally:
            stop_server(process)
    (args.output_dir / "export_browser_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
