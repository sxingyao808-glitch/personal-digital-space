from pathlib import Path
import io
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.backup import ArchiveBackup, ArchiveRestorer
from archive.cold_backup import cold_backup
from archive.instance_lock import DataDirLock, DataDirLockedError
from archive.release import ReleaseRegistry, public_content_id
from archive.store import ArchiveStore


class BackupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "source"
        app = create_app(self.data, testing=True)
        self.registry = app.extensions["release_registry"]
        self.item = ArchiveStore(self.data).create_content("Synthetic published item", "body", "public")
        AttachmentStore(self.data).upload(io.BytesIO(b"synthetic attachment"),
            filename="attachment.txt", operation_id="op_p3", attachment_id="f_p3",
            content_id=self.item["id"], visibility="public")
        self.release = self.registry.build([self.item["id"]], ["f_p3"])["release_id"]
        self.registry.activate(self.release)

    def test_cold_lock_covers_copy_verify_and_commit_and_allows_retry(self):
        out = self.root / "copy"
        source_copy, rename = shutil.copyfile, __import__("os").rename
        observations = []

        def checked_copy(src, dst):
            with self.assertRaises(DataDirLockedError):
                DataDirLock(self.data).acquire()
            observations.append("copy")
            return source_copy(src, dst)

        def checked_rename(src, dst):
            with self.assertRaises(DataDirLockedError):
                DataDirLock(self.data).acquire()
            self.assertTrue((src / "manifest.json").is_file())
            observations.append("commit")
            return rename(src, dst)

        self.registry._arm_withdrawal("synthetic interruption")
        with patch("archive.cold_backup.shutil.copyfile", side_effect=OSError("test write failure")):
            with self.assertRaises(OSError):
                cold_backup(self.data, out)
        self.assertFalse(out.exists())
        with patch("archive.cold_backup.shutil.copyfile", side_effect=checked_copy), \
             patch("archive.cold_backup.os.rename", side_effect=checked_rename):
            receipt = cold_backup(self.data, out)
        self.assertEqual(observations[-1], "commit")
        self.assertIn("copy", observations)
        self.assertEqual(Path(receipt["source_dir"]), self.data.resolve())
        with DataDirLock(self.data):
            pass
        self.assertFalse((out / ".app.lock").exists())
        self.assertEqual((out / "objects/f_p3").read_bytes(), b"synthetic attachment")
        self.assertEqual(ReleaseRegistry(out).list()["active_release_id"], self.release)
        self.assertTrue(ReleaseRegistry(out).withdrawal_pending())

    def test_healthy_pending_and_legacy_registry_restore(self):
        archive = self.root / "core.zip"
        ArchiveBackup(self.data).create(archive)
        normal = self.root / "normal.zip"
        self.registry.backup(normal)
        legacy = self.root / "legacy.zip"
        with zipfile.ZipFile(normal) as source, zipfile.ZipFile(legacy, "w") as dest:
            manifest = json.loads(source.read("manifest.json"))
            manifest["format_version"] = 3
            manifest.pop("withdrawal_pending")
            dest.writestr("manifest.json", json.dumps(manifest))
            for name in source.namelist():
                if name != "manifest.json":
                    dest.writestr(name, source.read(name))
        self.registry._arm_withdrawal("synthetic interruption")
        pending = self.root / "pending.zip"
        self.registry.backup(pending)
        for package, closed, status in ((normal, False, 200), (legacy, True, 503), (pending, True, 503)):
            data = self.root / package.stem
            ArchiveRestorer(archive).restore(data)
            result = ReleaseRegistry(data).restore_backup(package)
            self.assertEqual(result["publication_closed"], closed)
            self.assertEqual(result["active_release_id"], self.release)
            route = "/api/public/content/" + public_content_id(self.item["id"])
            self.assertEqual(create_app(data).test_client().get(route).status_code, status)
        mixed = self.root / "mixed"
        ArchiveRestorer(archive).restore(mixed)
        ArchiveStore(mixed).update_content(self.item["id"], self.item["revision"], visibility="private")
        self.assertTrue(ReleaseRegistry(mixed).restore_backup(normal)["publication_closed"])
