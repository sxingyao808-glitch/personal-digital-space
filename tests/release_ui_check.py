"""P4 Playwright UI check for the owner release-management page.

Runs against an isolated temporary data directory with synthetic content.
Produces JSON + screenshots under ``evidence/p4-ui``. Requires Playwright
with an installed Chromium-compatible browser (Edge channel is used here).
"""

from __future__ import annotations

import io
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

TOKEN = "p4-ui-owner-token-13579"
SHOTS = ROOT / "evidence" / "p4-ui"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed(app):
    client = app.test_client()
    client.get(f"/auth?token={TOKEN}")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    headers = {"X-CSRF-Token": csrf}

    def create(title, body, visibility="public"):
        response = client.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=headers,
        )
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]

    first = create("第一篇文章", "第一篇的公开正文。")
    second = create("第二篇文章", "第二篇的公开正文。")
    private = create("私密文章", "不应出现在发布选择中。", "private")
    xss = create(
        "安全 <img src=x onerror=\"window.__xss=2\"> 标题",
        "<script>window.__xss=1</script>正文原样显示。",
    )
    upload = client.post(
        f"/api/content/{first['id']}/attachments",
        data={
            "file": (io.BytesIO(b"ui check attachment"), "note.txt"),
            "operation_id": "op_p4_ui_seed",
            "visibility": "public",
        },
        content_type="multipart/form-data",
        headers=headers,
    )
    assert upload.status_code == 201, upload.get_data(as_text=True)
    attachment = upload.get_json()["data"]["attachment"]

    def prepare(content_ids, attachment_ids):
        response = client.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids},
            headers=headers,
        )
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]["release_id"]

    def activate(release_id):
        response = client.post(
            f"/api/releases/{release_id}/activate", json={}, headers=headers
        )
        assert response.status_code == 200, response.get_data(as_text=True)

    release_a = prepare([first["id"]], [attachment["id"]])
    activate(release_a)
    release_b = prepare([first["id"], second["id"]], [])
    activate(release_b)
    return {
        "first": first["id"],
        "second": second["id"],
        "private": private["id"],
        "xss": xss["id"],
        "release_a": release_a,
        "release_b": release_b,
    }


def build_server(app):
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def wait_ready(base_url, timeout=12.0):
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/login", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def main() -> int:
    from playwright.sync_api import expect, sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    results = {"steps": [], "screenshots": [], "ok": False}

    def step(name, detail=""):
        results["steps"].append({"step": name, "ok": True, "detail": detail})

    with tempfile.TemporaryDirectory() as tempdir:
        data_dir = Path(tempdir) / "data"
        app = create_app(data_dir, bootstrap_token=TOKEN)
        fixture = seed(app)
        server, base = build_server(app)
        wait_ready(base)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    # Guest isolation -----------------------------------------
                    guest = browser.new_context(viewport={"width": 1360, "height": 900})
                    guest_page = guest.new_page()
                    guest_page.goto(f"{base}/releases")
                    expect(guest_page).to_have_url(f"{base}/login")
                    step("访客访问 /releases 被重定向到登录页", guest_page.url)
                    guest.close()

                    context = browser.new_context(viewport={"width": 1360, "height": 900})
                    page = context.new_page()
                    page.set_default_timeout(14000)
                    diagnostics = []
                    page.on("pageerror", lambda exc: diagnostics.append(str(exc)))
                    page.goto(f"{base}/auth?token={TOKEN}")
                    expect(page).to_have_url(f"{base}/")

                    # Overview --------------------------------------------
                    page.goto(f"{base}/releases")
                    expect(page.locator("#release-current-chip")).to_have_text("本机发布成功")
                    expect(page.locator("#release-history-list .release-row")).to_have_count(2)
                    expect(page.locator("#release-history-total")).to_contain_text("共 2 条")
                    page.screenshot(path=str(SHOTS / "01-overview.png"), full_page=True)
                    results["screenshots"].append("01-overview.png")
                    step("发布管理首页加载当前公开状态与 2 条历史")

                    # Detail + integrity -----------------------------------
                    page.locator("#release-current-body summary").click()
                    page.locator("#release-current-body button").click()
                    expect(page.locator("#release-drawer")).to_be_visible()
                    expect(page.locator("#release-detail-content")).to_contain_text("基本信息")
                    expect(page.locator("#release-detail-content")).to_contain_text("来源与后继")
                    page.locator("#release-detail-verify").click()
                    expect(page.locator("#release-detail-state")).to_have_text("完整性通过")
                    page.screenshot(path=str(SHOTS / "02-detail.png"), full_page=False)
                    results["screenshots"].append("02-detail.png")
                    page.locator("#release-detail-close").click()
                    expect(page.locator("#release-drawer")).to_be_hidden()
                    step("Release 详情抽屉与按需完整性校验通过")

                    # History filters -------------------------------------
                    page.locator("#release-history > summary").click()
                    page.locator("#release-history > details > summary").click()
                    page.select_option("#filter-status", "superseded")
                    page.locator("#filter-apply").click()
                    expect(page.locator("#release-history-list .release-row")).to_have_count(1)
                    expect(page.locator("#release-history-status")).to_contain_text("共 1 条")
                    step("状态筛选 superseded 生效")
                    page.locator("#filter-reset").click()
                    expect(page.locator("#release-history-list .release-row")).to_have_count(2)

                    # Publish -----------------------------------------------
                    page.locator("#release-advanced").evaluate("(node) => { node.open = true; }")
                    page.locator("#release-tab-publish").evaluate("(node) => { node.open = true; }")
                    publish_cards = page.locator("#publish-candidates .release-pick")
                    expect(publish_cards).to_have_count(3)
                    first_card = publish_cards.filter(has_text="第一篇文章")
                    first_card.locator('input[type="checkbox"]').first.check()
                    expect(first_card).to_contain_text("note.txt")
                    safe_card = publish_cards.filter(has_text="安全")
                    safe_card.locator('input[type="checkbox"]').first.check()
                    expect(page.locator("#publish-selection-summary")).to_contain_text("已选择 2 个内容")
                    page.locator("#publish-preview-button").click()
                    expect(page.locator("#publish-preview-result")).to_contain_text("预览有效")
                    expect(page.locator("#publish-preview-result")).to_contain_text(
                        "未生成 Bundle"
                    )
                    page.screenshot(path=str(SHOTS / "03-publish-preview.png"), full_page=True)
                    results["screenshots"].append("03-publish-preview.png")

                    page.locator("#publish-submit-button").click()
                    expect(page.locator("#release-confirm-dialog")).to_be_visible()
                    page.locator("#release-confirm-ok").click()
                    expect(page.locator("#publish-preview-result")).to_contain_text("发布成功")
                    expect(page.locator("#release-current-body")).to_contain_text("公开内容")
                    step("发布流程 prepare → activate 完成")

                    # XSS safety -----------------------------------------
                    page.locator("#release-current-body summary").click()
                    page.locator("#release-current-body button").click()
                    expect(page.locator("#release-detail-content")).to_contain_text("安全")
                    xss_value = page.evaluate("window.__xss")
                    if xss_value is not None:
                        raise AssertionError("动态标题/正文中的脚本被执行")
                    step("动态文本按纯文本渲染，未执行脚本")

                    page.locator("#release-detail-close").click()

                    # Withdraw -------------------------------------------
                    page.locator("#release-operations").evaluate("(node) => { node.open = true; }")
                    page.locator('[data-release-tab="withdraw"]').click()
                    expect(page.locator("#withdraw-candidates .release-pick")).to_have_count(2)
                    withdraw_first = page.locator("#withdraw-candidates .release-pick").filter(
                        has_text="第一篇文章"
                    )
                    withdraw_first.locator('input[type="checkbox"]').first.check()
                    expect(page.locator("#withdraw-summary")).to_contain_text("将撤回 1 个内容")
                    page.locator("#withdraw-submit-button").click()
                    expect(page.locator("#release-confirm-dialog")).to_be_visible()
                    page.locator("#release-confirm-ok").click()
                    expect(page.locator("#withdraw-result")).to_contain_text("撤回成功")
                    expect(page.locator("#withdraw-candidates .release-pick")).to_have_count(1)
                    step("部分撤回生成后继 Release")

                    # Restore --------------------------------------------
                    page.locator('[data-release-tab="restore"]').click()
                    page.select_option("#restore-source", fixture["release_b"])
                    page.locator("#restore-load-button").click()
                    expect(page.locator("#restore-candidates")).to_contain_text("第二篇文章")
                    page.screenshot(path=str(SHOTS / "04-restore-source.png"), full_page=True)
                    results["screenshots"].append("04-restore-source.png")
                    page.locator("#restore-preview-button").click()
                    expect(page.locator("#restore-preview-result")).to_contain_text("dry-run 完成")
                    expect(page.locator("#restore-withdrawn-wrap")).to_be_visible()
                    page.locator("#restore-withdrawn-confirm").check()
                    page.locator("#restore-final-confirm").check()
                    expect(page.locator("#restore-submit-button")).to_be_enabled()
                    page.locator("#restore-submit-button").click()
                    expect(page.locator("#release-confirm-dialog")).to_be_visible()
                    page.locator("#release-confirm-ok").click()
                    expect(page.locator("#restore-result")).to_contain_text("恢复成功")
                    step("历史恢复 dry-run、撤回内容确认与提交成功")
                    page.screenshot(path=str(SHOTS / "05-restore-result.png"), full_page=True)
                    results["screenshots"].append("05-restore-result.png")

                    # Uncertain prepare ----------------------------------
                    before = page.evaluate(
                        "fetch('/api/releases').then(r => r.json()).then(j => j.data.release_count)"
                    )
                    page.locator("#release-advanced").evaluate("(node) => { node.open = true; }")
                    page.locator("#release-tab-publish").evaluate("(node) => { node.open = true; }")
                    publish_cards = page.locator("#publish-candidates .release-pick")
                    publish_cards.filter(has_text="第二篇文章").locator(
                        'input[type="checkbox"]'
                    ).first.check()
                    page.locator("#publish-preview-button").click()
                    expect(page.locator("#publish-preview-result")).to_contain_text("预览有效")
                    page.route("**/api/releases/prepare", lambda route: route.abort())
                    page.locator("#publish-submit-button").click()
                    page.locator("#release-confirm-ok").click()
                    expect(page.locator("#publish-preview-result")).to_contain_text("结果不确定")
                    expect(page.locator("#publish-preview-result")).to_contain_text("不会自动重试")
                    page.unroute("**/api/releases/prepare")
                    after = page.evaluate(
                        "fetch('/api/releases').then(r => r.json()).then(j => j.data.release_count)"
                    )
                    if after != before:
                        raise AssertionError("网络失败时不应生成 Release")
                    step("prepare 网络不确定时不自动重试、不生成 Release")

                    # Mobile layout --------------------------------------
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.reload()
                    expect(page.locator("#release-current-chip")).to_have_text("本机发布成功")
                    overflow = page.evaluate(
                        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                    )
                    if overflow > 2:
                        raise AssertionError(f"移动端出现横向滚动：{overflow}px")
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(SHOTS / "06-mobile.png"), full_page=True)
                    results["screenshots"].append("06-mobile.png")
                    step("移动端布局无横向溢出")

                    if diagnostics:
                        raise AssertionError("页面脚本错误：" + "; ".join(diagnostics))
                    context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()

    results["ok"] = True
    (SHOTS / "playwright-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
