from pathlib import Path
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.auth import OwnerSecretError, ensure_secret
from archive.backup import ArchiveBackup, ArchiveRestorer, BackupError, RestoreError, audit_data_dir
from archive.db import SchemaVersionError, init_db, transaction
from archive.export import ArchiveExporter, ExportError
from archive.store import ArchiveStore


class Phase3Contracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "data"
        self.app = create_app(self.data, testing=True, bootstrap_token="synthetic-p3")
        self.content = ArchiveStore(self.data).create_content("Synthetic archive")
        AttachmentStore(self.data).upload(io.BytesIO(b"normal attachment"), filename="x.txt",
            operation_id="op_p3", attachment_id="f_p3", content_id=self.content["id"])

    def test_invalid_key_and_creation_race_do_not_return_secret(self):
        root = self.root / "keys"
        root.mkdir()
        path = root / "owner_secret"
        valid = ensure_secret(root)
        self.assertEqual(ensure_secret(root), valid)
        path.write_bytes(b"short-invalid")
        with self.assertRaises(OwnerSecretError) as raised:
            ensure_secret(root)
        self.assertNotIn("short-invalid", str(raised.exception))
        path.unlink()

        def race(src, dst):
            dst.write_text("too-short", encoding="utf-8")
            raise FileExistsError

        with patch("archive.auth.os.link", side_effect=race), self.assertRaises(OwnerSecretError):
            ensure_secret(root)

    def test_write_mismatch_refused_by_backup_and_export(self):
        real_write = zipfile.ZipFile.write

        def faulty(archive, filename, arcname=None, **kwargs):
            if Path(filename).name == "f_p3":
                return archive.writestr(arcname, b"synthetic wrong output")
            return real_write(archive, filename, arcname, **kwargs)

        for name, error, method in (("core", BackupError, ArchiveBackup(self.data).create),
                                    ("export", ExportError, ArchiveExporter(self.data).export)):
            with self.subTest(name=name):
                path = self.root / (name + ".zip")
                with patch.object(zipfile.ZipFile, "write", faulty), self.assertRaises(error):
                    method(path)
                self.assertFalse(path.exists())

    def test_atomic_output_refuses_competing_target(self):
        target = self.root / "core.zip"
        link = os.link

        def competitor(source, destination):
            destination.write_bytes(b"existing result")
            link(source, destination)

        with patch("archive.package_io.os.link", side_effect=competitor), self.assertRaises(BackupError):
            ArchiveBackup(self.data).create(target)
        self.assertEqual(target.read_bytes(), b"existing result")

    def test_schema2_missing_spaces_refused_and_ready_entries_cross_checked(self):
        package = self.root / "core.zip"
        ArchiveBackup(self.data).create(package)
        restored = self.root / "restored"
        self.assertTrue(ArchiveRestorer(package).restore(restored)["verified"])
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        for change in ("missing", "ownership", "digest"):
            with self.subTest(change=change):
                altered = json.loads(json.dumps(manifest))
                if change == "missing":
                    altered["attachments"] = []
                else:
                    field = {"ownership": "content_id", "digest": "sha256"}[change]
                    altered["attachments"][0][field] = "fixture mismatch"
                self.assertFalse(audit_data_dir(restored, altered)["ok"])
        with transaction(restored / "archive.db") as conn:
            conn.execute("DROP TABLE content_spaces")
        with self.assertRaises(SchemaVersionError):
            init_db(restored / "archive.db")
        self.assertFalse(audit_data_dir(restored, manifest)["ok"])

    def test_release_confirm_requires_explicit_bool_at_both_routes(self):
        client = self.app.test_client()
        client.get("/auth?token=synthetic-p3")
        csrf = client.get("/api/session").json["data"]["csrf"]
        headers = {"X-CSRF-Token": csrf}
        registry = self.app.extensions["release_registry"]
        for endpoint, name in (("/api/releases/restore", "restore"),
                               ("/api/releases/rel_fixture/activate", "activate")):
            before = registry.list()
            with patch.object(registry, name, return_value={}) as operation:
                bad = client.post(endpoint, json={"confirm_republish_withdrawn": "false"}, headers=headers)
                self.assertIn(bad.status_code, (400, 422))
                operation.assert_not_called()
                self.assertEqual(registry.list(), before)
                for payload, expected in (({}, False), ({"confirm_republish_withdrawn": True}, True),
                                          ({"confirm_republish_withdrawn": False}, False)):
                    self.assertEqual(client.post(endpoint, json=payload, headers=headers).status_code, 200)
                    self.assertIs(operation.call_args.kwargs["confirm_republish_withdrawn"], expected)
