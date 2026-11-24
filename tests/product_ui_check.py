"""V1 product completion UI check (Playwright/Edge).

Runs against an isolated temporary data directory with synthetic content only.
Verifies the HOME entry panel, space panels, reading state, empty states,
trash behaviour and the mobile browsing path. Writes evidence under
``evidence/v1-product-completion``.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.serving import make_server

from archive.app import create_app

TOKEN = "v1-product-ui-owner-token-13579"
SHOTS = ROOT / "evidence" / "v1-product-completion"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed(app) -> dict:
    client = app.test_client()
    client.get(f"/auth?token={TOKEN}")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    headers = {"X-CSRF-Token": csrf}

    def create(title, body, **model):
        payload = {"title": title, "body": body, "visibility": "private", **model}
        response = client.post("/api/content", json=payload, headers=headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]

    me = create(
        "虚构观测日志",
        "气球观测站记录了一次虚构的云层观测。",
        type="EXPERIENCE",
        primary_space="ME",
        lifecycle="COMPLETED",
    )
    site = create(
        "月面花园",
        "月面花园使用虚构的灌溉控制器。",
        type="PROJECT",
        primary_space="BUILD",
        lifecycle="PUBLISHED",
        related_spaces=["THINK"],
    )
    thought = create(
        "水下钟表",
        "水下钟表可以在合成潮汐数据上演示。",
        type="THOUGHT",
        primary_space="THINK",
        lifecycle="ARCHIVED",
    )
    removed = create(
        "一条被删除的记录",
        "用于验证回收站与恢复路径。",
        type="THOUGHT",
        primary_space="ME",
    )
    relation = client.post(
        f"/api/content/{site['id']}/relations",
        json={"to_id": me["id"], "type": "REFERENCES"},
        headers=headers,
    )
    assert relation.status_code == 201, relation.get_data(as_text=True)
    deleted = client.delete(
        f"/api/content/{removed['id']}",
        json={"revision": removed["revision"]},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    return {"me": me, "site": site, "thought": thought, "removed": removed}


def main() -> int:
    from playwright.sync_api import expect, sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    results = {
        "ok": False,
        "steps": [],
        "console": [],
        "page_errors": [],
        "screenshots": [],
    }

    def step(name, detail=""):
        results["steps"].append({"step": name, "ok": True, "detail": detail})

    def shot(page, name):
        page.wait_for_timeout(600)
        path = SHOTS / name
        page.screenshot(path=str(path), full_page=False)
        results["screenshots"].append(str(path.relative_to(ROOT)))

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        app = create_app(data_dir, bootstrap_token=TOKEN, testing=True)
        fixtures = seed(app)
        port = free_port()
        server = make_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"

        deadline = time.time() + 10
        import urllib.request

        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/login", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not become ready")

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                page.on(
                    "console",
                    lambda msg: results["console"].append(
                        {"type": msg.type, "text": msg.text}
                    ),
                )
                page.on("pageerror", lambda exc: results["page_errors"].append(str(exc)))
                page.goto(f"{base}/auth?token={TOKEN}")
                page.wait_for_selector("#overview-panel:not(.hidden)", timeout=15000)
                expect(page.locator("#overview-title")).to_have_text("个人数字空间")
                cards = page.locator(".space-card")
                expect(cards).to_have_count(6)
                counts = [
                    cards.nth(index).locator(".space-card-count").inner_text()
                    for index in range(cards.count())
                ]
                assert counts == ["1", "1", "2", "0", "0", "1"], counts
                expect(page.locator(".overview-item")).to_have_count(3)
                shot(page, "01-overview-desktop.png")
                step("home_entry_panel", {"cards": counts, "recent": 3})

                page.locator(".space-card[data-space='BUILD']").click()
                expect(page.locator("#overview-title")).to_have_text("创造")
                expect(page.locator("#list-label")).to_have_text("创造 · 1 条")
                expect(page.locator(".overview-item")).to_have_count(1)
                shot(page, "02-space-build.png")
                step("space_panel", {"space": "BUILD", "items": 1})

                page.locator(".content-item").first.click()
                expect(page.locator("#reader-view")).to_be_visible()
                expect(page.locator("#edit-view")).to_be_hidden()
                expect(page.locator("#reader-title")).to_have_text("月面花园")
                expect(page.locator("#reader-meta")).to_contain_text("项目")
                expect(page.locator("#reader-meta")).to_contain_text("已发布")
                expect(page.locator("#reader-body")).to_contain_text("虚构的灌溉控制器")
                expect(page.locator("#edit-button")).to_be_visible()
                shot(page, "03-reader.png")
                step("reader_default", {"title": "月面花园"})

                page.locator("#edit-button").click()
                expect(page.locator("#edit-view")).to_be_visible()
                expect(page.locator("#reader-view")).to_be_hidden()
                expect(page.locator("#title-input")).to_have_value("月面花园")
                page.locator("#summary-input").fill("编辑后的摘要，用于验证保存回流。")
                page.locator("#body-input").fill("这是一段用于验证长文保存的长内容。\n" * 80)
                page.evaluate("window.scrollTo(0, 600)")
                expect(page.locator("#save-button")).to_be_in_viewport()
                shot(page, "09-edit-sticky-save.png")
                page.locator("#save-button").click()
                expect(page.locator("#reader-view")).to_be_visible(timeout=15000)
                expect(page.locator("#reader-summary")).to_have_text(
                    "编辑后的摘要，用于验证保存回流。"
                )
                step("edit_save_returns_to_reader")

                page.locator("#close-button").click()
                expect(page.locator("#overview-panel")).to_be_visible()
                expect(page.locator("#overview-title")).to_have_text("创造")

                page.locator(".space-button[data-space='HOME']").click()
                page.fill("#search-input", "不存在的关键词")
                expect(page.locator("#list-empty-text")).to_contain_text("没有找到")
                expect(page.locator("#list-empty-action")).to_have_text("清除搜索")
                shot(page, "04-search-empty.png")
                page.locator("#list-empty-action").click()
                expect(page.locator("#list-empty")).to_be_hidden()
                step("search_empty_state")

                page.locator(".space-button[data-space='JOURNEY']").click()
                expect(page.locator("#overview-title")).to_have_text("经历")
                expect(page.locator("#list-empty-text")).to_have_text("经历还没有内容。")
                expect(page.locator("#list-empty-action")).to_have_text("在经历新建")
                page.locator("#list-empty-action").click()
                expect(page.locator("#edit-view")).to_be_visible()
                expect(page.locator("#primary-space-input")).to_have_value("JOURNEY")
                page.locator("#title-input").fill("第一次经历记录")
                page.locator("#body-input").fill("从空间的空状态直接新建，用于验证创建路径。")
                page.locator("#save-button").click()
                expect(page.locator("#reader-view")).to_be_visible(timeout=15000)
                expect(page.locator("#reader-title")).to_have_text("第一次经历记录")
                shot(page, "05-space-empty.png")
                step("space_empty_state", {"space": "JOURNEY"})

                page.locator("#close-button").click()
                expect(page.locator("#overview-title")).to_have_text("经历")
                expect(page.locator(".content-item", has_text="第一次经历记录")).to_be_visible()
                page.locator(".space-button[data-space='HOME']").click()
                expect(page.locator(".space-card[data-space='JOURNEY'] .space-card-count")).to_have_text("1")
                step("create_from_space_empty_state")

                page.goto(base)
                page.wait_for_selector("#overview-panel:not(.hidden)", timeout=15000)
                page.locator("#trash-toggle").check()
                expect(page.locator(".content-item", has_text="一条被删除的记录")).to_be_visible()
                page.locator(".content-item", has_text="一条被删除的记录").click()
                expect(page.locator("#readonly-banner")).to_be_visible()
                expect(page.locator("#restore-button")).to_be_visible()
                expect(page.locator("#edit-button")).to_be_hidden()
                expect(page.locator("#reader-title")).to_have_text("一条被删除的记录")
                shot(page, "06-trash-readonly.png")
                page.locator("#restore-button").click()
                expect(page.locator("#restore-button")).to_be_hidden(timeout=15000)
                step("trash_readonly_and_restore")

                page.locator("#trash-toggle").uncheck()
                page.wait_for_timeout(500)
                first = page.locator(".content-item").first
                first.focus()
                first.press("ArrowDown")
                focused = page.evaluate("document.activeElement.dataset.id")
                second_id = page.locator(".content-item").nth(1).get_attribute("data-id")
                assert focused == second_id, (focused, second_id)
                step("keyboard_list_navigation")

                page.fill("#search-input", "月面花园")
                page.wait_for_timeout(600)
                expect(page.locator(".content-item")).to_have_count(1)
                expect(page.locator(".content-item").first).to_contain_text("月面花园")
                page.fill("#search-input", "")
                page.wait_for_timeout(600)
                step("search_finds_content")

                page.locator(".space-button[data-space='ARCHIVE']").click()
                expect(page.locator("#overview-title")).to_have_text("归档")
                expect(page.locator(".content-item", has_text="水下钟表")).to_be_visible()
                page.locator(".content-item", has_text="水下钟表").click()
                expect(page.locator("#reader-view")).to_be_visible()
                page.locator("#edit-button").click()
                page.select_option("#lifecycle-input", "PUBLISHED")
                page.locator("#save-button").click()
                expect(page.locator("#reader-view")).to_be_visible(timeout=15000)
                expect(page.locator("#reader-meta")).to_contain_text("已发布")
                page.locator("#close-button").click()
                expect(page.locator("#list-empty-text")).to_contain_text("还没有归档的内容")
                shot(page, "10-archive-empty.png")

                page.locator(".space-button[data-space='THINK']").click()
                expect(page.locator(".content-item", has_text="水下钟表")).to_be_visible()
                page.locator(".content-item", has_text="水下钟表").click()
                page.locator("#edit-button").click()
                page.select_option("#lifecycle-input", "ARCHIVED")
                page.locator("#save-button").click()
                expect(page.locator("#reader-view")).to_be_visible(timeout=15000)
                expect(page.locator("#reader-meta")).to_contain_text("已归档")
                page.locator("#close-button").click()
                page.select_option("#lifecycle-filter", "ARCHIVED")
                page.wait_for_timeout(600)
                expect(page.locator(".content-item", has_text="水下钟表")).to_have_count(1)
                page.select_option("#lifecycle-filter", "PUBLISHED")
                page.wait_for_timeout(600)
                expect(page.locator(".content-item", has_text="水下钟表")).to_have_count(0)
                page.select_option("#lifecycle-filter", "")
                page.wait_for_timeout(600)

                page.locator(".space-button[data-space='ARCHIVE']").click()
                expect(page.locator(".content-item", has_text="水下钟表")).to_be_visible()
                step("lifecycle_and_archive_rediscovery")

                page.locator(".space-button[data-space='BUILD']").click()
                page.locator(".content-item", has_text="月面花园").click()
                page.locator("#edit-button").click()
                page.locator('[data-tab="attachments"]').click()
                sample = Path(tmp) / "note.txt"
                sample.write_text("v1 product completion attachment", encoding="utf-8")
                page.locator("#attachment-input").set_input_files(str(sample))
                expect(page.locator("#attachments-list .record")).to_have_count(1, timeout=15000)
                expect(page.locator("#attachments-list .record")).to_contain_text("note.txt")
                expect(page.locator("#attachments-list .record")).to_contain_text("私有附件")
                expect(page.locator("#attachments-empty")).to_be_hidden()
                page.locator("#close-button").click()
                expect(page.locator("#overview-panel")).to_be_visible()
                step("attachment_upload_private")

                guest_context = browser.new_context(viewport={"width": 1000, "height": 800})
                guest = guest_context.new_page()
                guest.goto(base + "/")
                assert "/login" in guest.url, guest.url
                private_page = guest.goto(f"{base}/p/{fixtures['site']['id']}")
                assert private_page is not None and private_page.status == 404
                api_guest = guest.request.get(f"{base}/api/content")
                assert api_guest.status == 401, api_guest.status
                guest_context.close()
                step("anonymous_isolation", {"login_redirect": True, "private_404": True})

                empty_dir = Path(tmp) / "first-visit-data"
                empty_app = create_app(empty_dir, bootstrap_token=TOKEN, testing=True)
                empty_port = free_port()
                empty_server = make_server("127.0.0.1", empty_port, empty_app)
                empty_thread = threading.Thread(target=empty_server.serve_forever, daemon=True)
                empty_thread.start()
                empty_base = f"http://127.0.0.1:{empty_port}"
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        with urllib.request.urlopen(empty_base + "/login", timeout=1) as response:
                            if response.status == 200:
                                break
                    except Exception:
                        time.sleep(0.1)
                fresh_context = browser.new_context(viewport={"width": 1440, "height": 900})
                fresh = fresh_context.new_page()
                fresh.goto(f"{empty_base}/auth?token={TOKEN}")
                expect(fresh.locator("#empty-state")).to_be_visible(timeout=15000)
                expect(fresh.locator("#empty-new-button")).to_have_text("写下第一条")
                expect(fresh.locator("#overview-panel")).to_be_hidden()
                fresh.screenshot(path=str(SHOTS / "11-first-visit.png"), full_page=False)
                results["screenshots"].append(
                    str((SHOTS / "11-first-visit.png").relative_to(ROOT))
                )
                fresh_context.close()
                empty_server.shutdown()
                empty_thread.join(timeout=5)
                step("first_visit_empty_state")

                mobile_context = browser.new_context(
                    viewport={"width": 390, "height": 844}
                )
                mobile = mobile_context.new_page()
                mobile.goto(f"{base}/auth?token={TOKEN}")
                mobile.wait_for_selector("#content-list li", timeout=15000)
                mobile.wait_for_timeout(400)
                assert mobile.evaluate("window.innerWidth") == 390
                box = mobile.locator("#content-list").bounding_box()
                assert box and box["height"] > 120, box
                overflow = mobile.evaluate(
                    "document.documentElement.scrollWidth - window.innerWidth"
                )
                assert overflow <= 1, overflow
                shot(mobile, "07-mobile-home.png")
                mobile.locator(".content-item").first.click()
                expect(mobile.locator("#reader-view")).to_be_visible()
                expect(mobile.locator(".sidebar")).to_be_hidden()
                shot(mobile, "08-mobile-reader.png")
                mobile.locator("#close-button").click()
                expect(mobile.locator(".sidebar")).to_be_visible()
                expect(mobile.locator("#content-list")).to_be_visible()
                step("mobile_browse", {"list_height": box["height"], "overflow": overflow})

                mobile_context.close()
                context.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)

    errors = [entry for entry in results["console"] if entry["type"] == "error"]
    if errors:
        results["errors"] = errors
        raise AssertionError(f"console errors: {errors}")
    if results["page_errors"]:
        raise AssertionError(f"page errors: {results['page_errors']}")
    results["ok"] = True
    (SHOTS / "ui-check.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
