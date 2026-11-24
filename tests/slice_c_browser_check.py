"""Slice C browser verification for the read-only integrity drawer.

The script seeds a disposable Archive, starts the real local Flask app on a
loopback port, signs in through the bootstrap token, and verifies that:

- non-READY attachment state/note is visible;
- the last reconciliation report is labelled as historical;
- quarantine inventory details are visible;
- no cleanup/delete/move/retry/retention action is offered.

It writes a JSON result and a drawer screenshot. It does not touch project data.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.store import ArchiveStore

TOKEN = "test-owner-token-slice-c-browser-135792468"
DISALLOWED_WORDS = ("清理", "删除", "移动", "重试", "保留")


def seed(data_dir: Path) -> dict:
    app = create_app(data_dir, bootstrap_token=TOKEN, testing=True)
    store = ArchiveStore(data_dir)
    attachments = AttachmentStore(data_dir)
    content = store.create_content("浏览器完整性检查", "私有正文", "private")
    result = attachments.upload(
        io.BytesIO(b"broken browser payload"),
        filename="broken-slice-c.txt",
        content_id=content["id"],
        operation_id="op_browser_broken",
        declared_mime="text/plain",
    )
    attachment_id = result["attachment"]["id"]
    conn = sqlite3.connect(data_dir / "archive.db")
    try:
        conn.execute(
            "UPDATE attachments SET state = 'BROKEN', note = '浏览器可见备注' WHERE id = ?",
            (attachment_id,),
        )
        conn.commit()
    finally:
        conn.close()
    (data_dir / "objects" / attachment_id).unlink(missing_ok=True)
    (data_dir / "objects" / "f_orphan_browser").write_bytes(b"orphan browser bytes")
    report = attachments.reconcile()
    return {
        "app": app,
        "content_id": content["id"],
        "attachment_id": attachment_id,
        "quarantine_inventory_count": report["quarantine_inventory_count"],
        "report_checked_at": report["checked_at"],
    }


def run(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "ok": False,
        "steps": [],
        "console": [],
        "page_errors": [],
        "request_failures": [],
    }
    tempdir = tempfile.TemporaryDirectory()
    server = None
    try:
        data_dir = Path(tempdir.name) / "data"
        seeded = seed(data_dir)
        app = seeded["app"]
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        result["base_url"] = base_url
        result["seed"] = {
            "content_id": seeded["content_id"],
            "attachment_id": seeded["attachment_id"],
            "quarantine_inventory_count": seeded["quarantine_inventory_count"],
            "report_checked_at": seeded["report_checked_at"],
        }
        assert seeded["quarantine_inventory_count"] == 1

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 960})
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
            page.locator("#integrity-button").click()
            page.wait_for_selector("#integrity-drawer:not(.hidden)")
            page.wait_for_selector("#integrity-content .integrity-section")

            drawer = page.locator("#integrity-content")
            expect(drawer).to_contain_text("非 READY 附件")
            expect(drawer).to_contain_text("broken-slice-c.txt")
            expect(drawer).to_contain_text("浏览器可见备注")
            expect(drawer).to_contain_text("上次核对（历史报告）")
            expect(drawer).to_contain_text("这是扫描时的历史报告")
            expect(drawer).to_contain_text("隔离清单（只读）")
            expect(drawer).to_contain_text("objects-f_orphan_browser")
            expect(drawer).to_contain_text("SHA-256")

            buttons = page.locator("#integrity-drawer button").all_text_contents()
            disallowed = [
                text
                for text in buttons
                if any(word in text for word in DISALLOWED_WORDS)
            ]
            assert not disallowed, f"unexpected action buttons: {disallowed}"

            screenshot = output_dir / "slice_c_integrity_drawer.png"
            page.locator("#integrity-drawer").screenshot(path=str(screenshot))
            result["drawer_text"] = drawer.inner_text()
            result["buttons"] = buttons
            result["screenshot"] = str(screenshot)
            result["steps"].append(
                {
                    "integrity_drawer": "pass",
                    "non_ready_attachment": "pass",
                    "historical_report_label": "pass",
                    "quarantine_inventory": "pass",
                    "no_cleanup_actions": "pass",
                }
            )

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            expect(drawer).to_contain_text("非 READY 附件")
            expect(drawer).to_contain_text("隔离清单（只读）")
            no_horizontal_overflow = page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            )
            assert no_horizontal_overflow, "mobile viewport has horizontal overflow"
            mobile_screenshot = output_dir / "slice_c_integrity_drawer_mobile.png"
            page.locator("#integrity-drawer").screenshot(path=str(mobile_screenshot))
            result["mobile_screenshot"] = str(mobile_screenshot)
            result["steps"][0]["responsive_drawer"] = "pass"
            browser.close()

        errors = [item for item in result["console"] if item["type"] == "error"]
        result["ok"] = (
            not errors
            and not result["page_errors"]
            and not result["request_failures"]
        )
        result["console_errors"] = errors
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["console_errors"] = [
            item for item in result["console"] if item["type"] == "error"
        ]
        return result
    finally:
        if server is not None:
            server.shutdown()
        tempdir.cleanup()
        (output_dir / "slice_c_browser_check.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slice C integrity drawer browser check")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
