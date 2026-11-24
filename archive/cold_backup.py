"""Stopped-archive copy using the same exclusion lock as the application."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid

from . import __version__
from .instance_lock import DataDirLock, DataDirLockedError
from .win_data import make_data_dir


def _inventory(root: Path) -> list[dict]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".app.lock":
            continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"归档含链接或重解析点：{relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"归档含非普通文件：{relative}")
        with path.open("rb") as stream:
            entries.append({"path": relative, "bytes": info.st_size,
                            "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
    return entries


def cold_backup(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not (source / "archive.db").is_file():
        raise ValueError("指定归档没有 archive.db。")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("备份目录和源归档必须分离。")
    repo = Path(__file__).resolve().parent.parent
    if destination.is_relative_to(repo):
        raise ValueError("备份目录不能位于源码仓库。")
    with DataDirLock(source):
        if destination.exists():
            raise FileExistsError("备份目标已存在，拒绝覆盖。")
        partial = destination.with_name(f"{destination.name}.partial-{uuid.uuid4().hex}")
        make_data_dir(partial)
        try:
            source_entries = _inventory(source)
            for entry in source_entries:
                target = partial / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / entry["path"], target)
            if _inventory(partial) != source_entries or _inventory(source) != source_entries:
                raise ValueError("副本校验失败或复制期间归档出现变化。")
            manifest = {"format": "personal-archive-cold-backup", "format_version": 1,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "program_version": __version__, "source_dir": str(source),
                        "file_count": len(source_entries),
                        "total_bytes": sum(item["bytes"] for item in source_entries),
                        "files": source_entries}
            with (partial / "manifest.json").open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if json.loads((partial / "manifest.json").read_text(encoding="utf-8")) != manifest:
                raise ValueError("备份清单无法重新校验。")
            # Windows rename will fail if a competing destination exists.
            os.rename(partial, destination)
            return {"ok": True, "source_dir": str(source), "backup_dir": str(destination),
                    "file_count": len(source_entries)}
        finally:
            if partial.is_dir():
                shutil.rmtree(partial)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(cold_backup(args.data_dir, args.backup_dir), ensure_ascii=False))
        return 0
    except DataDirLockedError:
        print("源归档正在使用，冷备份未开始。", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"冷备份未完成：{exc}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
