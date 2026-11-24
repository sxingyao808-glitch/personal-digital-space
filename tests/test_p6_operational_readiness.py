"""P6 operational-readiness tests.

Covers three P6 capabilities, all against temporary data directories with
purely synthetic content:

* single-instance data-dir locking (:mod:`archive.instance_lock`)
* release-registry backup (``--registry-backup``), including failure cleanup
* the strictly read-only publication drift report (P6-C) and its owner-only API
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.release as release_module
import run_app
from archive.app import create_app
from archive.instance_lock import DataDirLock, DataDirLockedError
from archive.release import (
    PUBLICATION_DRIFT_CATEGORIES,
    ReleaseRegistry,
    public_content_id,
)
from archive.store import ArchiveError

TOKEN = "test-owner-token-p6-975318642"

HOLDER_SCRIPT = """
import sys, time
sys.path.insert(0, {root!r})
from archive.instance_lock import DataDirLock, DataDirLockedError
lock = DataDirLock({data_dir!r})
try:
    lock.acquire()
except DataDirLockedError:
    print("LOCKED", flush=True)
    sys.exit(0)
print("ACQUIRED", flush=True)
time.sleep({hold})
lock.release()
print("RELEASED", flush=True)
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_lock_payload(path: Path) -> dict:
    """Read the diagnostic range without opening a Python file object.

    Windows denies ``open()`` on a file whose byte range is locked, even from
    the same process, so tests read it the same way the lock implementation
    does.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    return json.loads(raw.decode("utf-8").strip() or "{}")


def tree_digest(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    if not root.exists():
        return {}
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == pattern or relative.endswith(pattern) for pattern in exclude):
            continue
        digests[relative] = sha256_file(path)
    return digests


class DataDirLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.data_dir = self.base / "data"
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _spawn(self, data_dir: Path, hold: float = 30) -> subprocess.Popen:
        script = HOLDER_SCRIPT.format(
            root=str(ROOT), data_dir=str(data_dir), hold=hold
        )
        # A Windows venv python.exe is a launcher that re-executes the real
        # interpreter as a child process. The lock is acquired by that child,
        # so Popen.pid would otherwise point at the launcher instead of the
        # holder. Spawning the base interpreter keeps Popen.pid authoritative.
        executable = getattr(sys, "_base_executable", None) or sys.executable
        return subprocess.Popen(
            [executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def _first_line(proc: subprocess.Popen) -> str:
        line = proc.stdout.readline().strip()
        return line

    def test_first_acquire_writes_diagnostic_and_release_leaves_file(self):
        lock = DataDirLock(self.data_dir)
        lock.acquire()
        try:
            self.assertTrue(lock.path.is_file())
            payload = read_lock_payload(lock.path)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["nonce"], lock.nonce)
        finally:
            lock.release()
        # The lock file is a stable anchor and is intentionally not deleted.
        self.assertTrue(lock.path.is_file())
        self.assertEqual(read_lock_payload(lock.path)["nonce"], lock.nonce)

    def test_same_process_cannot_acquire_twice(self):
        lock = DataDirLock(self.data_dir)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                lock.acquire()
        finally:
            lock.release()

    def test_second_instance_same_dir_is_rejected(self):
        proc = self._spawn(self.data_dir)
        try:
            self.assertEqual(self._first_line(proc), "ACQUIRED")
            with self.assertRaises(DataDirLockedError) as caught:
                DataDirLock(self.data_dir).acquire()
            self.assertEqual(caught.exception.code, "data_dir_locked")
            self.assertEqual(caught.exception.details.get("holder_pid"), proc.pid)
            self.assertIn("占用", caught.exception.message)
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_different_dirs_run_independently(self):
        other = self.base / "other"
        other.mkdir()
        lock = DataDirLock(self.data_dir).acquire()
        proc = self._spawn(other, hold=30)
        try:
            self.assertEqual(self._first_line(proc), "ACQUIRED")
            self.assertTrue((other / ".app.lock").is_file())
            self.assertTrue(lock.path.is_file())
        finally:
            proc.kill()
            proc.wait(timeout=15)
            lock.release()

    def test_normal_exit_releases_lock(self):
        proc = self._spawn(self.data_dir, hold=0.3)
        try:
            self.assertEqual(self._first_line(proc), "ACQUIRED")
            self.assertEqual(self._first_line(proc), "RELEASED")
            proc.wait(timeout=15)
        finally:
            proc.kill()
            proc.wait(timeout=15)
        lock = DataDirLock(self.data_dir).acquire()
        lock.release()

    def test_hard_kill_releases_lock(self):
        proc = self._spawn(self.data_dir)
        try:
            self.assertEqual(self._first_line(proc), "ACQUIRED")
            proc.kill()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
        # Windows tears the byte-range lock down with the process; no stale
        # PID probing or manual file deletion is required.
        lock = DataDirLock(self.data_dir).acquire()
        try:
            self.assertEqual(lock.previous_holder.get("pid"), proc.pid)
        finally:
            lock.release()

    def test_stale_diagnostic_payload_never_blocks_and_is_replaced(self):
        # A leftover JSON payload (e.g. PID reuse or crash) is diagnostic only:
        # the OS lock, not the content, decides ownership.
        fake = {"pid": 999999, "nonce": "deadbeef", "started_at": "2020-01-01T00:00:00"}
        lock_path = self.data_dir / ".app.lock"
        lock_path.write_text(json.dumps(fake), encoding="utf-8")
        lock = DataDirLock(self.data_dir).acquire()
        try:
            self.assertEqual(lock.previous_holder, fake)
            payload = read_lock_payload(lock_path)
            self.assertEqual(payload["nonce"], lock.nonce)
            self.assertEqual(payload["pid"], os.getpid())
        finally:
            lock.release()
        # Releasing never deletes the shared anchor.
        self.assertTrue(lock_path.is_file())

    def test_concurrent_start_has_exactly_one_winner(self):
        procs = [self._spawn(self.data_dir) for _ in range(5)]
        results: list[str] = []
        try:
            for proc in procs:
                results.append(self._first_line(proc))
            self.assertEqual(results.count("ACQUIRED"), 1, results)
            self.assertEqual(results.count("LOCKED"), 4, results)
        finally:
            for proc in procs:
                proc.kill()
                proc.wait(timeout=15)


class ReleaseRegistryBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)
        self.registry: ReleaseRegistry = self.app.extensions["release_registry"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def login(self, client) -> dict[str, str]:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return {"X-CSRF-Token": csrf}

    def create_content(self, title="备份条目", body="正文"):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": "public"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def publish(self) -> dict:
        content = self.create_content()
        prepared = self.registry.build([content["id"]], [])
        self.registry.activate(prepared["release_id"])
        return prepared

    def test_backup_creates_integrity_checked_zip(self):
        prepared = self.publish()
        target = Path(self.tempdir.name) / "registry-backup.zip"
        result = self.registry.backup(target)

        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["path"]), target.resolve())
        self.assertEqual(result["sha256"], sha256_file(target))
        self.assertEqual(result["active_release_id"], prepared["release_id"])
        self.assertEqual(result["release_count"], 1)
        boundaries = result["boundaries"]
        self.assertTrue(boundaries["includes_release_registry"])
        self.assertFalse(boundaries["includes_private_database"])
        self.assertFalse(boundaries["includes_attachment_objects"])
        self.assertFalse(boundaries["includes_owner_secret"])
        self.assertFalse(boundaries["includes_lock_file"])

        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertIn("registry/index.json", names)
        self.assertIn("registry/events.jsonl", names)
        self.assertIn(f"registry/{prepared['release_id']}/release.json", names)
        self.assertIn(
            f"registry/{prepared['release_id']}/content/"
            f"{public_content_id(prepared['content_ids'][0])}.html",
            names,
        )
        self.assertEqual(manifest["format"], "personal-archive-release-registry-backup")
        self.assertEqual(len(manifest["files"]), result["file_count"])
        for name in names:
            self.assertNotIn("archive.db", name)
            self.assertNotIn("objects/", name)
            self.assertNotIn("owner_secret", name)
            self.assertNotIn(".app.lock", name)
            self.assertFalse(name.endswith(".lock"))

    def test_backup_default_location_is_release_backups(self):
        self.publish()
        result = self.registry.backup(None)
        path = Path(result["path"])
        self.assertEqual(path.parent, (self.data_dir / "release-backups").resolve())
        self.assertTrue(path.is_file())

    def test_backup_refuses_to_overwrite_existing_target(self):
        self.publish()
        target = Path(self.tempdir.name) / "existing.zip"
        target.write_bytes(b"do-not-touch")
        with self.assertRaises(ArchiveError) as caught:
            self.registry.backup(target)
        self.assertIn("拒绝覆盖", caught.exception.message)
        self.assertEqual(target.read_bytes(), b"do-not-touch")

    def test_rejects_target_inside_releases_dir(self):
        self.publish()
        target = self.data_dir / "releases" / "backup.zip"
        with self.assertRaises(ArchiveError):
            self.registry.backup(target)
        self.assertFalse(target.exists())

    def test_failed_backup_leaves_no_partial_zip_or_temp_file(self):
        self.publish()
        out_dir = Path(self.tempdir.name) / "out"
        out_dir.mkdir()
        target = out_dir / "failed.zip"
        with mock.patch.object(
            release_module,
            "_verify_registry_backup",
            side_effect=release_module.ReleaseError("模拟校验失败"),
        ):
            with self.assertRaises(ArchiveError):
                self.registry.backup(target)
        self.assertFalse(target.exists())
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_cli_registry_backup_emits_report_and_notice(self):
        self.publish()
        target = Path(self.tempdir.name) / "cli-registry.zip"
        report = Path(self.tempdir.name) / "cli-registry-report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = run_app.main(
                [
                    "--data-dir",
                    str(self.data_dir),
                    "--registry-backup",
                    str(target),
                    "--report",
                    str(report),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kind"], "release_registry")
        self.assertIn("不能替代 --backup", payload["notice"])
        self.assertEqual(payload["sha256"], sha256_file(target))

    def test_cli_rejects_combined_backup_actions(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                run_app.main(
                    [
                        "--data-dir",
                        str(self.data_dir),
                        "--backup",
                        "--registry-backup",
                    ]
                )


class PublicationDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)
        self.registry: ReleaseRegistry = self.app.extensions["release_registry"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def login(self, client) -> dict[str, str]:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return {"X-CSRF-Token": csrf}

    def create_content(self, title="公开条目", body="正文", visibility="public"):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def patch_content(self, content_id, revision, **fields):
        response = self.owner.patch(
            f"/api/content/{content_id}",
            json={"revision": revision, **fields},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def delete_content(self, content_id, revision):
        response = self.owner.delete(
            f"/api/content/{content_id}",
            json={"revision": revision},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]["content"]

    def upload(self, content_id, payload=b"p6 attachment bytes\n", filename="note.txt",
               visibility="public"):
        response = self.owner.post(
            f"/api/content/{content_id}/attachments",
            data={
                "file": (io.BytesIO(payload), filename),
                "operation_id": f"op_{os.urandom(12).hex()}",
                "visibility": visibility,
            },
            content_type="multipart/form-data",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]["attachment"]

    def set_attachment_visibility(self, attachment_id, visibility):
        response = self.owner.patch(
            f"/api/attachments/{attachment_id}",
            json={"visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def publish(self, content_ids, attachment_ids=None):
        prepared = self.registry.build(content_ids, attachment_ids or [])
        self.registry.activate(prepared["release_id"])
        return prepared

    def drift(self):
        response = self.owner.get("/api/publication/drift")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def category_of(self, report, content_id):
        return [row["category"] for row in report["contents"] if row["content_id"] == content_id]

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_summary_covers_all_documented_categories(self):
        report = self.drift()
        self.assertEqual(set(report["summary"]), set(PUBLICATION_DRIFT_CATEGORIES))
        self.assertTrue(report["read_only"])
        self.assertEqual(report["state"], "verified")

    def test_no_active_release_marks_selected_content_not_published(self):
        content = self.create_content()
        self.registry.build([content["id"]], [])
        report = self.drift()
        self.assertIsNone(report["active_release_id"])
        self.assertEqual(self.category_of(report, content["id"]), ["NOT_IN_ACTIVE_RELEASE"])
        self.assertEqual(report["privacy_risk_count"], 0)

    def test_published_and_current_is_not_a_risk(self):
        content = self.create_content()
        self.publish([content["id"]])
        report = self.drift()
        self.assertEqual(self.category_of(report, content["id"]), ["PUBLISHED_AND_CURRENT"])
        self.assertEqual(report["privacy_risk_count"], 0)
        self.assertEqual(report["stale_count"], 0)
        self.assertTrue(report["bundle_verified"])

    def test_edit_after_publish_is_stale_not_privacy_risk(self):
        content = self.create_content("原标题", "原正文")
        self.publish([content["id"]])
        self.patch_content(content["id"], content["revision"], title="新标题", body="新正文")
        report = self.drift()
        self.assertEqual(
            self.category_of(report, content["id"]), ["PUBLISHED_SNAPSHOT_STALE"]
        )
        self.assertEqual(report["stale_count"], 1)
        self.assertEqual(report["privacy_risk_count"], 0)

    def test_content_changed_to_private_is_privacy_risk(self):
        content = self.create_content()
        self.publish([content["id"]])
        self.patch_content(content["id"], content["revision"], visibility="private")
        report = self.drift()
        self.assertEqual(
            self.category_of(report, content["id"]), ["PUBLISHED_BUT_NOW_PRIVATE"]
        )
        self.assertEqual(report["privacy_risk_count"], 1)

    def test_content_deselected_is_reported(self):
        content = self.create_content()
        self.publish([content["id"]])
        self.patch_content(content["id"], content["revision"], visibility=None)
        report = self.drift()
        self.assertEqual(
            self.category_of(report, content["id"]), ["PUBLISHED_BUT_DESELECTED"]
        )
        self.assertEqual(report["privacy_risk_count"], 1)

    def test_deleted_content_is_privacy_risk(self):
        content = self.create_content()
        self.publish([content["id"]])
        self.delete_content(content["id"], content["revision"])
        report = self.drift()
        self.assertEqual(
            self.category_of(report, content["id"]), ["PUBLISHED_BUT_DELETED"]
        )
        self.assertEqual(report["privacy_risk_count"], 1)

    def test_attachment_visibility_change_is_attachment_drift(self):
        content = self.create_content()
        attachment = self.upload(content["id"])
        self.publish([content["id"]], [attachment["id"]])
        self.set_attachment_visibility(attachment["id"], "private")
        report = self.drift()
        drifted = [
            item for item in report["attachments"] if item["attachment_id"] == attachment["id"]
        ]
        self.assertEqual(len(drifted), 1, report["attachments"])
        self.assertIn("附件当前为私有", drifted[0]["reasons"])
        self.assertEqual(report["summary"]["PUBLISHED_ATTACHMENT_DRIFT"], 1)
        self.assertEqual(report["privacy_risk_count"], 1)

    def test_healthy_attachment_is_not_reported(self):
        content = self.create_content()
        attachment = self.upload(content["id"])
        self.publish([content["id"]], [attachment["id"]])
        report = self.drift()
        self.assertEqual(report["attachments"], [])
        self.assertEqual(report["summary"]["PUBLISHED_ATTACHMENT_DRIFT"], 0)

    def test_selected_public_content_missing_from_active_release(self):
        published = self.create_content("已发布")
        self.publish([published["id"]])
        new_public = self.create_content("新公开")
        report = self.drift()
        self.assertEqual(
            self.category_of(report, new_public["id"]), ["SELECTED_BUT_NOT_PUBLISHED"]
        )
        self.assertEqual(self.category_of(report, published["id"]), ["PUBLISHED_AND_CURRENT"])

    def test_private_content_is_never_listed_in_drift(self):
        private = self.create_content(visibility="private")
        report = self.drift()
        self.assertEqual([row["content_id"] for row in report["contents"]], [])
        self.assertNotIn(private["id"], json.dumps(report))

    def test_visitor_cannot_read_drift(self):
        content = self.create_content()
        self.publish([content["id"]])
        anonymous = self.app.test_client()
        response = anonymous.get("/api/publication/drift")
        self.assertEqual(response.status_code, 401)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_drift_is_strictly_read_only(self):
        content = self.create_content()
        attachment = self.upload(content["id"])
        self.publish([content["id"]], [attachment["id"]])
        self.patch_content(content["id"], content["revision"], title="已编辑")

        before_releases = tree_digest(self.data_dir / "releases")
        before_objects = tree_digest(self.data_dir / "objects")
        before_db = sha256_file(self.data_dir / "archive.db")
        before_secret = sha256_file(self.data_dir / "owner_secret")
        index_before = json.loads(
            (self.data_dir / "releases" / "index.json").read_text(encoding="utf-8")
        )
        events_before = (self.data_dir / "releases" / "events.jsonl").read_text(
            encoding="utf-8"
        )

        first = self.drift()
        second = self.drift()

        self.assertEqual(tree_digest(self.data_dir / "releases"), before_releases)
        self.assertEqual(tree_digest(self.data_dir / "objects"), before_objects)
        self.assertEqual(sha256_file(self.data_dir / "archive.db"), before_db)
        self.assertEqual(sha256_file(self.data_dir / "owner_secret"), before_secret)
        index_after = json.loads(
            (self.data_dir / "releases" / "index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(index_after["active_release_id"], index_before["active_release_id"])
        self.assertEqual(len(index_after["releases"]), len(index_before["releases"]))
        self.assertEqual(
            (self.data_dir / "releases" / "events.jsonl").read_text(encoding="utf-8"),
            events_before,
        )
        for key in ("active_release_id", "release_count", "summary"):
            self.assertEqual(first[key], second[key])

    def test_vanished_attachment_object_is_reported(self):
        content = self.create_content()
        attachment = self.upload(content["id"])
        self.publish([content["id"]], [attachment["id"]])
        (self.data_dir / "objects" / attachment["id"]).unlink()
        report = self.drift()
        self.assertEqual(report["summary"]["PUBLISHED_ATTACHMENT_DRIFT"], 1)
        reasons = report["attachments"][0]["reasons"]
        self.assertIn("附件对象文件缺失", reasons)

    def test_corrupted_registry_is_reported_as_uncertain(self):
        content = self.create_content()
        self.publish([content["id"]])
        index_path = self.data_dir / "releases" / "index.json"
        index_path.write_text("{not-json", encoding="utf-8")
        report = self.drift()
        self.assertEqual(report["state"], "uncertain")
        self.assertTrue(report["warnings"])
        self.assertEqual(report["summary"]["PUBLISHED_AND_CURRENT"], 0)

    def test_owner_pages_expose_drift_panels(self):
        content = self.create_content()
        self.publish([content["id"]])
        releases_page = self.owner.get("/releases")
        self.assertEqual(releases_page.status_code, 200)
        html = releases_page.get_data(as_text=True)
        self.assertIn('id="release-drift-body"', html)
        self.assertIn("公开副本一致性", html)

        index_page = self.owner.get("/")
        self.assertEqual(index_page.status_code, 200)
        self.assertIn('id="publication-drift-note"', index_page.get_data(as_text=True))

        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/releases").status_code, 302)


if __name__ == "__main__":
    unittest.main()
