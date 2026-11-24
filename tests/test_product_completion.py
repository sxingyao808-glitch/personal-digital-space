"""V1 product completion: archive overview + owner page contract.

The overview endpoint is read-only and only feeds the HOME / space panels.
It must not change archive state, and its counts must match the existing
``list_content`` semantics used by the list itself.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import init_db
from archive.store import ArchiveStore

TOKEN = "test-owner-token-product-completion-99887766"


class OverviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        init_db(self.data_dir / "archive.db")
        self.store = ArchiveStore(self.data_dir)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_counts_match_list_content_semantics(self):
        self.store.create_content(
            "关于我", "正文", "private", content_type="EXPERIENCE", primary_space="ME"
        )
        self.store.create_content(
            "项目", "正文", "private", content_type="PROJECT",
            primary_space="BUILD", related_spaces=["THINK"],
        )
        self.store.create_content(
            "旧想法", "正文", "private", content_type="THOUGHT",
            primary_space="THINK", lifecycle="ARCHIVED",
        )

        overview = self.store.overview()
        self.assertEqual(overview["total"], 3)
        for space in ("ME", "BUILD", "THINK", "JOURNEY", "COMPETE"):
            self.assertEqual(
                overview["spaces"][space],
                len(self.store.list_content(space=space)),
                f"space count mismatch for {space}",
            )
        self.assertEqual(overview["spaces"]["THINK"], 2)
        self.assertEqual(overview["spaces"]["JOURNEY"], 0)

    def test_archive_is_a_lifecycle_entry_not_a_primary_space(self):
        self.store.create_content(
            "已归档的项目", "正文", "private", content_type="PROJECT",
            primary_space="BUILD", lifecycle="ARCHIVED",
        )
        overview = self.store.overview()
        self.assertEqual(overview["archived"], 1)
        self.assertEqual(overview["spaces"]["BUILD"], 1)
        self.assertEqual(overview["total"], 1)

    def test_deleted_content_is_excluded_and_recent_is_bounded(self):
        first = self.store.create_content(
            "第一条", "正文", "private", content_type="THOUGHT", primary_space="THINK"
        )
        for index in range(6):
            self.store.create_content(
                f"条目 {index}", "正文", "private",
                content_type="THOUGHT", primary_space="THINK",
            )
        self.store.update_content(
            first["id"], first["revision"], title="第一条（更新）"
        )
        removed = self.store.create_content(
            "将被删除", "正文", "private", content_type="THOUGHT", primary_space="ME"
        )
        self.store.delete_content(removed["id"], removed["revision"])

        overview = self.store.overview(recent_limit=3)
        self.assertEqual(overview["total"], 7)
        self.assertEqual(len(overview["recent"]), 3)
        self.assertEqual(overview["recent"][0]["title"], "第一条（更新）")
        self.assertNotIn(
            "将被删除", [item["title"] for item in overview["recent"]]
        )

    def test_overview_is_read_only(self):
        self.store.create_content(
            "内容", "正文", "private", content_type="PROJECT", primary_space="BUILD"
        )
        before = self.store.list_content(include_deleted=True)
        self.store.overview()
        after = self.store.list_content(include_deleted=True)
        self.assertEqual(before, after)


class OverviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        response = self.owner.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with self.owner.session_transaction() as session:
            self.csrf = session["csrf"]
        self.headers = {"X-CSRF-Token": self.csrf}
        self.anonymous = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_owner_reads_overview_and_anonymous_is_rejected(self):
        created = self.owner.post(
            "/api/content",
            json={
                "title": "私密项目",
                "body": "正文",
                "visibility": "private",
                "type": "PROJECT",
                "primary_space": "BUILD",
            },
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)

        response = self.owner.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["spaces"]["BUILD"], 1)
        self.assertEqual(data["recent"][0]["id"], created.get_json()["data"]["id"])
        for key in ("total", "spaces", "archived", "recent"):
            self.assertIn(key, data)

        self.assertEqual(self.anonymous.get("/api/overview").status_code, 401)


class OwnerPageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.owner.get(f"/auth?token={TOKEN}")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_page_exposes_entry_panel_reader_and_localized_filters(self):
        html = self.owner.get("/").get_data(as_text=True)
        for marker in (
            'id="overview-panel"',
            'id="overview-spaces"',
            'id="reader-view"',
            'id="edit-view"',
            'id="edit-button"',
            'id="list-empty-action"',
            "全部类型",
            "已归档",
            "关于我",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
