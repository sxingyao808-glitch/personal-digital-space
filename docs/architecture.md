# Architecture

The Python launcher opens a user-owned data directory and holds its instance lock. Flask listens on `127.0.0.1`. In the Electron app the shell supplies a generated bootstrap token, waits for a token-free readiness message, and loads the loopback UI. The backend stops when its owning shell exits. Browser-only use runs the same service directly.

| Area | Source | Responsibilities |
| --- | --- | --- |
| Local service | `run_app.py`, `archive/app.py`, `archive/auth.py` | Startup, owner session, request validation, and local HTTP API |
| Archive | `archive/db.py`, `archive/store.py`, `archive/attachments.py` | SQLite, migrations, content and relationships, and attachment files |
| Publication | `archive/release.py`, `archive/release_reader.py` | Review, snapshot assembly, activation, rollback, withdrawal, and anonymous reads |
| Online hosting | `archive/pages_hosting.py`, `archive/cloudflare_pages.py` | Explicitly configured Cloudflare Pages publication and status checks |
| Preservation | `archive/backup.py`, `archive/cold_backup.py`, `archive/export.py` | Backups, recovery, and export |
| Desktop | `desktop/electron/`, `desktop/scripts/` | Desktop shell, sidecar and Windows installer configuration |

## Publication boundary

Owner pages and state-changing APIs use a local owner session. Content and attachments start private. Making a record eligible for publication is distinct from activating a release. A release applies the public projection to the selected records and eligible attachments. Anonymous site requests read the active bundle rather than querying the owner archive. The configured online publishing operation sends static assets to the selected hosting target only when confirmed.

## Backup boundary

An archive backup preserves private content and attachment objects with a verified manifest. Keep it in private storage. Registry backups preserve local releases and withdrawal state separately. Restore the archive to a fresh directory, then restore its registry while the archive is stopped. If release state is uncertain, the restore flow can leave anonymous access closed.
