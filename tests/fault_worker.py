"""Subprocess worker used to prove crash recovery after a hard process exit.

Exit code 73 is produced by the attachment fault injector. It is intentionally
not a Python exception: the process must die without unwinding, so that only
durable journal state remains.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--content-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--fault", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", required=True, type=int)
    args = parser.parse_args()

    os.environ["ARCHIVE_TEST_MODE"] = "1"
    os.environ["ARCHIVE_FAULT_INJECT"] = args.fault

    from archive.attachments import AttachmentStore

    store = AttachmentStore(args.data_dir)
    with open(args.file, "rb") as handle:
        store.upload(
            handle,
            filename="crash-payload.bin",
            content_id=args.content_id,
            operation_id=args.operation_id,
            attachment_id=args.attachment_id,
            declared_mime="application/octet-stream",
            expected_sha256=args.sha256,
            expected_size=args.size,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
