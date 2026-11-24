"""P5 Playwright check: first-version integration, responsive layout, focus.

Runs against an isolated temporary data directory with synthetic content.
Writes JSON and screenshots under ``evidence/first-version-acceptance``.
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

TOKEN = "p5-ui-owner-token-24680"
SHOTS = ROOT / "evidence" / "first-version-acceptance"


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

    def create(title, body, visibility="public"):
        response = client.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=headers,
        )
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]

    first = create("第一篇手记", "公开正文，记录一次构建过程。")
    second = create("第二篇手记", "另一篇公开正文。")
    private = create("私密草稿", "不应出现在公开页面。", "private")
    upload = client.post(
        f"/api/content/{first['id']}/attachments",
        data={
            "file": (io.BytesIO(b"first-version attachment"), "note.txt"),
            "operation_id": "op_p5_ui_seed",
            "visibility": "public",
        },
        content_type="multipart/form-data",
        headers=headers,
    )
    assert upload.status_code == 201, upload.get_data(as_text=True)
    attachment = upload.get_json()["data"]["attachment"]

    prepared = client.post(
        "/api/releases/prepare",
        json={"content_ids": [first["id"]], "attachment_ids": [attachment["id"]]},
        headers=headers,
    )
    assert prepared.status_code == 201, prepared.get_data(as_text=True)
    release_id = prepared.get_json()["data"]["release_id"]
    activation = client.post(
        f"/api/releases/{release_id}/activate", json={}, headers=headers
    )
    assert activation.status_code == 200, activation.get_data(as_text=True)
    return {
        "first": first["id"],
        "second": second["id"],
        "private": private["id"],
        "release_id": release_id,
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


MEASURE_JS = """
() => {
  const rect = (node) => {
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height};
  };
  const overlaps = (a, b) => {
    if (!a || !b) return false;
    return a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  };
  const brand = rect(document.querySelector('.brand'));
  const nav = rect(document.querySelector('.top-nav'));
  const actions = rect(document.querySelector('.top-actions'));
  const topbar = rect(document.querySelector('.topbar'));
  return {
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    topbar: topbar,
    brand: brand,
    nav: nav,
    actions: actions,
    brandActionsOverlap: overlaps(brand, actions),
    navActionsOverlap: overlaps(nav, actions),
    navFullRow: topbar && nav ? Math.abs(nav.width - (topbar.width - 56)) < 40 : false,
  };
}
"""


def collect(page, results, label) -> dict:
    measure = page.evaluate(MEASURE_JS)
    measure["label"] = label
    results["layout"].append(measure)
    return measure


def top(page):
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(200)


def main() -> int:
    from playwright.sync_api import expect, sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    results = {
        "ok": False,
        "steps": [],
        "layout": [],
        "screenshots": [],
        "console": [],
        "page_errors": [],
        "request_failures": [],
    }

    def step(name, detail=""):
        results["steps"].append({"step": name, "ok": True, "detail": detail})

    def shot(page, name):
        top(page)
        path = SHOTS / name
        page.screenshot(path=str(path), full_page=True)
        results["screenshots"].append(name)

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
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 960}
                    )
                    page = context.new_page()
                    page.on(
                        "console",
                        lambda message: results["console"].append(
                            {"type": message.type, "text": message.text}
                        )
                        if message.type in ("error", "warning")
                        else None,
                    )
                    page.on(
                        "pageerror",
                        lambda error: results["page_errors"].append(str(error)),
                    )
                    page.on(
                        "requestfailed",
                        lambda request: results["request_failures"].append(
                            {"url": request.url, "failure": request.failure}
                        ),
                    )

                    page.goto(f"{base}/auth?token={TOKEN}", wait_until="networkidle")
                    page.goto(base + "/", wait_until="networkidle")
                    expect(page.locator("#content-list .content-item").first).to_be_visible()
                    nav_links = page.locator(".top-nav .nav-link")
                    expect(nav_links).to_have_count(2)
                    expect(nav_links.nth(0)).to_have_text("内容")
                    expect(nav_links.nth(1)).to_have_text("发布")
                    expect(nav_links.nth(0)).to_have_attribute("aria-current", "page")
                    step("内容工作台导航包含 内容/发布", page.url)
                    measure = collect(page, results, "home-1440")
                    assert measure["scrollWidth"] <= measure["innerWidth"] + 1, measure
                    assert not measure["brandActionsOverlap"], measure
                    shot(page, "01-home-1440.png")

                    page.locator(".top-nav .nav-link", has_text="发布").click()
                    expect(page).to_have_url(base + "/releases")
                    expect(page.locator("h1")).to_contain_text("发布管理")
                    expect(page.locator(".top-nav .nav-link").nth(1)).to_have_attribute(
                        "aria-current", "page"
                    )
                    expect(page.locator("#release-current-body")).to_contain_text(
                        "激活时间"
                    )
                    expect(page.locator("#release-current-body")).to_contain_text(
                        fixture["release_id"]
                    )
                    collect(page, results, "releases-1440")
                    shot(page, "02-releases-1440.png")
                    step("发布管理页激活状态与导航正确", page.url)

                    # Detail drawer opens and verifies the bundle.
                    page.locator("#release-history-list .release-row button").first.click()
                    expect(page.locator("#release-drawer")).to_be_visible()
                    expect(page.locator("#release-detail-content")).to_contain_text(
                        "Release"
                    )
                    page.locator("#release-detail-verify").click()
                    expect(page.locator("#release-detail-state")).to_contain_text(
                        "完整", timeout=10000
                    )
                    page.keyboard.press("Escape")
                    expect(page.locator("#release-drawer")).to_be_hidden()
                    step("详情抽屉与完整性校验", "Escape 关闭")

                    # Return path back to the content workspace.
                    page.locator(".top-nav .nav-link", has_text="内容").click()
                    expect(page).to_have_url(base + "/")
                    page.locator(
                        "#content-list .content-item", has_text="第一篇手记"
                    ).click()
                    expect(page.locator("#editor-panel:not(.hidden)")).to_be_visible()
                    expect(page.locator("#public-link")).to_have_attribute(
                        "href", f"/p/{fixture['first']}"
                    )
                    step("从发布页返回内容工作台", "公开页链接可用")

                    # Public page in the owner browser and in a guest context.
                    public = context.new_page()
                    public.goto(f"{base}/p/{fixture['first']}", wait_until="networkidle")
                    expect(public.locator(".public-title")).to_contain_text("第一篇手记")
                    expect(public.locator(".attachment-link")).to_contain_text("note.txt")
                    shot(public, "03-public-1440.png")

                    guest = browser.new_context(viewport={"width": 390, "height": 844})
                    guest_page = guest.new_page()
                    visitor = guest_page.goto(
                        f"{base}/p/{fixture['first']}", wait_until="networkidle"
                    )
                    assert visitor is not None and visitor.status == 200
                    expect(guest_page.locator(".public-title")).to_contain_text(
                        "第一篇手记"
                    )
                    collect(guest_page, results, "public-390")
                    shot(guest_page, "06-public-390.png")

                    guest_page.goto(f"{base}/p/{fixture['private']}")
                    expect(guest_page.locator(".notfound-shell")).to_be_visible()
                    expect(guest_page.locator(".notfound-back a")).to_have_text(
                        "返回 Archive"
                    )
                    shot(guest_page, "07-notfound-390.png")

                    guest_page.locator(".notfound-back a").click()
                    expect(guest_page).to_have_url(base + "/login")
                    step("访客隔离与 404 返回路径", guest_page.url)

                    guest_page.goto(f"{base}/releases")
                    expect(guest_page).to_have_url(base + "/login")
                    step("访客访问 /releases 被重定向", guest_page.url)
                    guest.close()

                    # Responsive layout sweep on both owner pages.
                    context.close()
                    for width in (1440, 900, 768, 620, 390):
                        ctx = browser.new_context(
                            viewport={"width": width, "height": 900}
                        )
                        resize_page = ctx.new_page()
                        resize_page.goto(f"{base}/auth?token={TOKEN}")
                        resize_page.goto(base + "/", wait_until="networkidle")
                        home = collect(resize_page, results, f"home-{width}")
                        assert home["scrollWidth"] <= home["innerWidth"] + 1, home
                        assert not home["brandActionsOverlap"], home
                        assert not home["navActionsOverlap"], home
                        if width <= 900:
                            assert home["navFullRow"], home
                        if width in (900, 390):
                            shot(resize_page, f"04-home-{width}.png")

                        resize_page.goto(base + "/releases", wait_until="networkidle")
                        expect(resize_page.locator("#release-history-list")).to_be_visible()
                        console = collect(resize_page, results, f"releases-{width}")
                        assert console["scrollWidth"] <= console["innerWidth"] + 1, console
                        assert not console["brandActionsOverlap"], console
                        assert not console["navActionsOverlap"], console
                        if width <= 900:
                            assert console["navFullRow"], console
                        if width in (900, 390):
                            shot(resize_page, f"05-releases-{width}.png")
                        ctx.close()
                    step("1440/900/768/620/390 响应式无溢出与顶栏无重叠")

                    # Keyboard focus visibility on the home navigation.
                    focus_ctx = browser.new_context(
                        viewport={"width": 1280, "height": 860}
                    )
                    focus_page = focus_ctx.new_page()
                    focus_page.goto(f"{base}/auth?token={TOKEN}")
                    focus_page.goto(base + "/", wait_until="networkidle")
                    focus_page.locator("body").click(position={"x": 5, "y": 600})
                    styles = []
                    for _ in range(6):
                        focus_page.keyboard.press("Tab")
                        styles.append(
                            focus_page.evaluate(
                                """() => {
                                  const node = document.activeElement;
                                  const style = getComputedStyle(node);
                                  return {
                                    tag: node.tagName,
                                    className: String(node.className || ''),
                                    outlineStyle: style.outlineStyle,
                                    outlineWidth: style.outlineWidth,
                                  };
                                }"""
                            )
                        )
                    visible = [
                        item
                        for item in styles
                        if item["outlineStyle"] != "none"
                        and item["outlineWidth"] not in ("0px", "")
                    ]
                    assert visible, styles
                    results["focus"] = styles
                    step("键盘 Tab 可见焦点", json.dumps(visible[:2], ensure_ascii=False))
                    focus_ctx.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()

    errors = [entry for entry in results["console"] if entry["type"] == "error"]
    results["console_errors"] = errors
    if errors:
        raise AssertionError(f"console errors: {errors}")
    if results["page_errors"]:
        raise AssertionError(f"page errors: {results['page_errors']}")
    results["ok"] = True
    return results


if __name__ == "__main__":
    try:
        output = main()
    except Exception as exc:  # noqa: BLE001 - report for evidence
        SHOTS.mkdir(parents=True, exist_ok=True)
        failure = {"ok": False, "error": str(exc)}
        (SHOTS / "playwright-results.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        sys.exit(1)
    (SHOTS / "playwright-results.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": True, "steps": len(output["steps"])}, ensure_ascii=False))
    sys.exit(0)
