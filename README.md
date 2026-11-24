# Personal Digital Space

A local-first personal digital space for organizing, connecting, preserving, and selectively publishing personal knowledge, projects, experiences, and ideas.

Keep an editable archive on your own device and deliberately publish a curated website from it. The same content model supports an ongoing project, a reflection, a record of an experience, and the relationships among them. The application UI is currently in Simplified Chinese.

## What you can do

- Organize projects, thoughts, and experiences by space and lifecycle. Search them, link related items, and add attributes with provenance.
- Attach files, check their integrity, and recover archived or soft-deleted content.
- Review an explicit public selection and its attachments before publishing a static snapshot. The local site works without a hosting account. Cloudflare Pages integration requires a separate configuration and explicit online publication.
- Export selected records or back up the archive and restore it to a separate directory. Back up the publication registry separately if you need to preserve local publication history and withdrawal state.

See [Architecture](docs/architecture.md) for the private and public boundaries and [Data model](docs/data-model.md) for schema and migration details.

## Run from source

Use CPython 3.14 with SQLite 3.51.3 or newer, a browser, and a writable local data directory. Source validation uses Windows x64 with CPython 3.14.10 and SQLite 3.54.1. The Python service does not require Electron or a hosted account.

From the checkout root on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_app.py --data-dir ./archive_data --no-open
```

Open the owner login URL printed at startup and keep it private. The server listens on loopback and selects an available port. Run with no `--data-dir` from the checkout root to use `./archive_data`. For a demonstration, add `--init-fixture` and use a fresh directory such as `./archive_data/demo`. This explicitly loads fictional records and attachments without activating a public site.

### Back up and restore

Stop the server before running these commands. Use new output names and a fresh restore target on a local filesystem that supports hard links.

```powershell
.\.venv\Scripts\python.exe run_app.py --data-dir ./archive_data --backup
.\.venv\Scripts\python.exe run_app.py --data-dir ./archive_data --registry-backup
.\.venv\Scripts\python.exe run_app.py --restore "<archive-backup.zip>" --target ./archive_data/restored
.\.venv\Scripts\python.exe run_app.py --data-dir ./archive_data/restored --restore-registry "<registry-backup.zip>"
```

The first two commands report the generated paths. Keep these packages private. After restoring, review the result and publication state before restarting or publishing.

## Desktop development and build

Use Windows x64, PowerShell 5.1 or newer, Node.js 24 and npm. Install the Python requirements above and run `npm.cmd ci --prefix desktop`. To launch the Electron shell from source:

```powershell
$env:ARCHIVE_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
npm.cmd --prefix desktop start
```

The desktop archive defaults to `%LOCALAPPDATA%\PersonalDigitalSpaceOSS\data`. An explicit `ARCHIVE_DATA_DIR` override must point to an existing absolute directory.

For a Windows x64 NSIS build, prepare an isolated, pip-enabled runtime using the CPython and SQLite archives and digests in [desktop/runtime-lock.json](desktop/runtime-lock.json). Replace the runtime's SQLite DLL with the verified one and set `PDS_BUILD_PYTHON` to that runtime's `python.exe`. Then run:

```powershell
npm.cmd --prefix desktop run build:installer
```

The build checks the CPython version and SQLite source ID and verifies the packaged DLL hash. The unsigned installer is written under `desktop/release/` for local development.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The standard suite uses temporary synthetic archives and mock hosting. A live Cloudflare test is opt-in and some filesystem cases require Windows symbolic-link permission. Keep live credentials out of ordinary test runs.

## Project status

V1.0 is a source release candidate. Isolated Windows installation, supported-version upgrade, uninstall, and multi-account acceptance remain pending. V1.0 does not guarantee removal of copies already cached or downloaded by third parties. Keep the owner service on loopback and publish the reviewed static site when you need a public website.

## License

The project license is pending owner approval. See [third-party dependencies](docs/third-party.md) for their separate terms.
