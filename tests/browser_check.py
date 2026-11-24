"""Browser verification against a running local Archive server.

Uses the installed Microsoft Edge through Playwright (no bundled browser
download). Produces screenshots and a JSON result for evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def run(base_url: str, token: str, output_dir: Path) -> dict:
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

        page.goto(f"{base_url}/auth?token={token}", wait_until="networkidle")
        page.wait_for_selector("#content-list .content-item")
        expect(page).to_have_title("Archive · 私人档案")

        items = page.locator("#content-list .content-item")
        count_before = items.count()
        assert count_before >= 4, f"expected at least 4 fixture items, got {count_before}"
        titles = page.locator("#content-list .item-title").all_text_contents()
        assert "你好，世界" in titles and "一次本地实验" in titles
        result["steps"].append({"fixture_list": "pass", "count": count_before})

        hello = page.locator("#content-list .content-item", has_text="你好，世界")
        hello_id = hello.get_attribute("data-id")
        experiment = page.locator("#content-list .content-item", has_text="一次本地实验")
        experiment_id = experiment.get_attribute("data-id")
        private_item = page.locator("#content-list .content-item", has_text="私密草稿")
        private_id = private_item.get_attribute("data-id")

        hello.click()
        page.wait_for_selector("#editor-panel:not(.hidden)")
        expect(page.locator("#content-state")).to_have_text("公开")
        public_href = page.locator("#public-link").get_attribute("href")
        assert public_href == f"/p/{hello_id}"
        assert page.locator("#content-id").text_content() == hello_id
        result["steps"].append(
            {"open_fixture": "pass", "id": hello_id, "state": "公开"}
        )

        page.locator("#new-button").click()
        page.locator("#title-input").fill("Playwright 验证记录")
        page.locator("#body-input").fill("通过 UI 创建并保存")
        page.locator('[data-visibility="private"]').click()
        page.locator("#save-button").click()
        expect(page.locator("#save-status")).to_contain_text("已保存", timeout=10000)
        expect(page.locator("#content-list .content-item")).to_have_count(count_before + 1)
        expect(page.locator("#content-state")).to_have_text("私有")
        saved_id = page.locator("#content-id").text_content()
        assert saved_id.startswith("c_")
        result["steps"].append(
            {
                "create_save": "pass",
                "id": saved_id,
                "save_status": page.locator("#save-status").text_content(),
            }
        )

        page.locator("#edit-button").click()
        page.locator('[data-tab="relations"]').click()
        page.select_option("#relation-target", label="你好，世界")
        page.select_option("#relation-type", "REFERENCES")
        page.locator("#add-relation-button").click()
        expect(page.locator("#relations-list .record")).to_have_count(1, timeout=10000)
        relation_text = page.locator("#relations-list .record").first.text_content()
        assert "REFERENCES" in relation_text
        result["steps"].append({"add_relation": "pass", "row": relation_text.strip()})

        page.locator('[data-tab="attributes"]').click()
        page.locator("#attribute-key").fill("验证属性")
        page.locator("#attribute-value").fill("UI 写入")
        page.select_option("#attribute-provenance", "ai_suggested")
        page.locator("#attribute-confirmed").check()
        page.locator("#add-attribute-button").click()
        attribute_row = page.locator("#attributes-list .record", has_text="验证属性")
        expect(attribute_row).to_have_count(1, timeout=10000)
        attribute_text = attribute_row.text_content()
        assert "AI 建议" in attribute_text
        assert "未确认" in attribute_text
        attribute_row.locator("button", has_text="确认").click()
        expect(attribute_row).to_contain_text("用户确认", timeout=10000)
        expect(attribute_row).to_contain_text("已确认")
        result["steps"].append(
            {
                "attribute_ai_not_auto_confirmed": "pass",
                "after_confirm": attribute_row.text_content().strip(),
            }
        )

        page.locator("#integrity-button").click()
        expect(page.locator("#integrity-drawer")).to_be_visible()
        page.locator("#reconcile-button").click()
        expect(page.locator("#reconcile-status")).not_to_have_text("正在扫描…", timeout=15000)
        reconcile_text = page.locator("#reconcile-status").text_content()
        page.locator("#integrity-close").click()
        expect(page.locator("#integrity-drawer")).to_be_hidden()
        result["steps"].append({"integrity_reconcile": "pass", "status": reconcile_text})

        page.screenshot(path=str(output_dir / "ui-desktop.png"), full_page=True)

        public_page = context.new_page()
        public_page.goto(f"{base_url}/p/{hello_id}", wait_until="networkidle")
        assert "这是我的第一个 Archive 条目" in public_page.locator(".public-body").text_content()
        assert "私密草稿" not in public_page.content()
        result["steps"].append({"public_page": "pass", "url": f"{base_url}/p/{hello_id}"})

        experiment_page = context.new_page()
        experiment_page.goto(f"{base_url}/p/{experiment_id}", wait_until="networkidle")
        experiment_html = experiment_page.content()
        assert "fixture_sample.txt" in experiment_html
        assert "private_note.txt" not in experiment_html
        experiment_page.screenshot(path=str(output_dir / "ui-public.png"), full_page=True)
        result["steps"].append({"public_attachment_gating": "pass"})

        anonymous = browser.new_context(viewport={"width": 1000, "height": 800})
        anonymous_page = anonymous.new_page()
        response = anonymous_page.goto(f"{base_url}/p/{private_id}")
        assert response is not None and response.status == 404
        api_response = anonymous_page.request.get(f"{base_url}/api/public/content/{private_id}")
        assert api_response.status == 404
        anonymous.close()
        result["steps"].append({"private_public_404": "pass"})

        page.bring_to_front()
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        overflow = page.evaluate(
            "() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})"
        )
        assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, overflow
        page.screenshot(path=str(output_dir / "ui-mobile.png"), full_page=True)
        result["steps"].append({"responsive_390x844": "pass", "measure": overflow})

        context.close()
        browser.close()

    errors = [entry for entry in result["console"] if entry["type"] == "error"]
    if errors:
        result["errors"] = errors
        raise AssertionError(f"console errors: {errors}")
    if result["page_errors"]:
        raise AssertionError(f"page errors: {result['page_errors']}")

    result["ok"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.base_url, args.token, args.output_dir)
    except Exception as exc:  # noqa: BLE001 - report for evidence
        args.output_dir.mkdir(parents=True, exist_ok=True)
        failure = {"ok": False, "error": str(exc)}
        (args.output_dir / "browser_check.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    (args.output_dir / "browser_check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
