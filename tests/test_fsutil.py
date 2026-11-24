"""B5 / R-05: directory durability must be confirmed, not assumed."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.fsutil as fsutil
from archive import attachments
from archive import release


class DirectoryFlushTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.folder = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_existing_writable_directory_is_flushed(self):
        # Exercises the real platform path: os.fsync on POSIX, CreateFileW +
        # FlushFileBuffers with FILE_FLAG_BACKUP_SEMANTICS on Windows.
        self.assertIs(fsutil.fsync_directory(self.folder), True)

    def test_missing_directory_reports_failure(self):
        missing = self.folder / "does-not-exist"
        with self.assertLogs("archive.fsutil", level="WARNING"):
            self.assertIs(fsutil.fsync_directory(missing), False)

    def test_posix_open_failure_is_not_reported_as_success(self):
        with mock.patch.object(
            fsutil.os, "open", side_effect=PermissionError(13, "denied")
        ):
            with self.assertLogs("archive.fsutil", level="WARNING"):
                self.assertIs(fsutil._fsync_directory_posix(self.folder), False)

    def test_posix_fsync_failure_is_not_reported_as_success(self):
        with mock.patch.object(fsutil.os, "open", return_value=7), mock.patch.object(
            fsutil.os, "fsync", side_effect=OSError(5, "io error")
        ), mock.patch.object(fsutil.os, "close"):
            with self.assertLogs("archive.fsutil", level="WARNING"):
                self.assertIs(fsutil._fsync_directory_posix(self.folder), False)

    def test_both_modules_share_the_platform_helper(self):
        self.assertIs(attachments._fsync_directory(self.folder), True)
        self.assertIs(release._fsync_directory(self.folder), True)


if __name__ == "__main__":
    unittest.main()
