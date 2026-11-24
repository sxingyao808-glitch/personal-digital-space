"""Verification and exclusive publication for generated ZIP files."""

import hashlib
import os
from pathlib import Path
import zipfile


def verify_written_zip(path: Path, manifest_bytes: bytes, entries: list[dict]) -> None:
    listed = {"manifest.json", *(item["path"] for item in entries)}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(listed) != len(entries) + 1 or len(names) != len(listed) or set(names) != listed:
            raise ValueError("归档包成员与清单不一致。")
        if archive.read("manifest.json") != manifest_bytes:
            raise ValueError("归档包写入的清单不一致。")
        for entry in entries:
            info = archive.getinfo(entry["path"])
            with archive.open(info) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry["sha256"] or info.file_size != entry["bytes"]:
                raise ValueError("归档包成员未通过字节校验。")


def publish_new_file(temporary: Path, target: Path) -> None:
    """Publish complete bytes atomically without taking an existing name."""
    os.link(temporary, target)
