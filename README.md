# Personal Digital Space

A local-first personal digital space for organizing, connecting, preserving, and selectively publishing personal knowledge, projects, experiences, and ideas.

Keep an editable archive on your own device and deliberately publish a curated website from it. The same content model supports an ongoing project, a reflection, a record of an experience, and the relationships among them. The application UI is currently in Simplified Chinese.

## Core features

- Organize projects, thoughts, and experiences by space and lifecycle. Search them, link related items, and add attributes with provenance.
- Attach files, check their integrity, and recover archived or soft-deleted content.
- Review an explicit public selection and its attachments before publishing a static snapshot. The local site works without a hosting account. Cloudflare Pages integration requires a separate configuration and explicit online publication.
- Export selected records or back up the archive and restore it to a separate directory. Back up the publication registry separately if you need to preserve local publication history and withdrawal state.

## Architecture and technology

The local service uses Python and Flask to serve the owner interface on loopback. SQLite stores archive records and the local filesystem holds attachments. The Electron shell starts and manages the same backend. Public pages come from a curated static snapshot. See [Architecture](docs/architecture.md) for the private and public boundaries and [Data model](docs/data-model.md) for schema and migration details.

| Path | Contents |
| --- | --- |
| `run_app.py` | Source launcher and backup and restore CLI |
| `archive/` | Local service, data model, and browser interface |
| `desktop/` | Electron shell, runtime lock, and Windows build tools |
| `docs/` | Architecture, data model, and third-party terms |
| `scripts/` | Windows launch, archive copy, and backup tools |
| `tests/` | Automated tests and synthetic fixtures |

## Requirements

Use CPython 3.14 with SQLite 3.51.3 or newer, a browser, and a writable local data directory. Source validation uses Windows x64 with CPython 3.14.10 and SQLite 3.54.1. The Python service does not require Electron or a hosted account.

## Installation

From the checkout root in Windows PowerShell, create a virtual environment and install the Python dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Quick start

```powershell
.\.venv\Scripts\python.exe run_app.py --data-dir ./archive_data --no-open
```

Open the owner login URL printed at startup and keep it private. The server listens on loopback and selects an available port. Run with no `--data-dir` from the checkout root to use `./archive_data`. For a demonstration, add `--init-fixture` and use a fresh directory such as `./archive_data/demo`. This explicitly loads fictional records and attachments without activating a public site.

## Data storage and privacy

The source launcher uses `./archive_data` relative to the working directory by default. The desktop shell defaults to `%LOCALAPPDATA%\PersonalDigitalSpaceOSS\data`. The archive keeps `archive.db` and attachment files inside the data directory. Cloning or sharing the source checkout does not automatically publish your archive. Keep the data directory, owner login URL, backups, and hosting configuration private.

## Publishing

Review the selected records and eligible attachments, then explicitly confirm publication to activate the static snapshot. Anonymous visitors can access the activated public snapshot. Online deployment to Cloudflare Pages requires configuring a hosting target and explicitly confirming the online action. See [the publication boundary](docs/architecture.md#publication-boundary).

## Backup and recovery

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

An explicit `ARCHIVE_DATA_DIR` override must point to an existing absolute directory.

For a Windows x64 NSIS build, prepare an isolated, pip-enabled runtime using the CPython and SQLite archives and digests in [desktop/runtime-lock.json](desktop/runtime-lock.json). Replace the runtime's SQLite DLL with the verified one and set `PDS_BUILD_PYTHON` to that runtime's `python.exe`. Then run:

```powershell
npm.cmd --prefix desktop run build:installer
```

The build checks the CPython version and SQLite source ID and verifies the packaged DLL hash. The unsigned installer is written under `desktop/release/` for local development.

## Testing and verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The standard suite uses temporary synthetic archives and mock hosting. A live Cloudflare test is opt-in and some filesystem cases require Windows symbolic-link permission. Keep live credentials out of ordinary test runs.

The V1.0 source candidate verification recorded 427 regression tests with 423 passed and four skipped. Three skipped tests require Windows symbolic-link permission and one requires opting in to live Cloudflare verification. The Electron source launch passed 18 checks, and the Windows x64 backend was rebuilt and verified with the locked CPython and SQLite versions.

## Known limitations

V1.0 is a source release. Independent Windows installation, upgrade, uninstall, and multi-account isolation acceptance remain pending. Previously downloaded or cached public copies can persist after withdrawal. Keep the owner service on loopback and publish the reviewed static site when you need a public website.

## Roadmap

The next distribution milestone is to complete independent Windows installation, upgrade, uninstall, and multi-account isolation acceptance before offering a verified installer.

## License

Personal Digital Space is available under the [MIT License](LICENSE). You may use, modify, distribute, and sell the software, including for commercial use. Include the copyright notice and license text when distributing copies or substantial portions of the software. See [third-party dependencies](docs/third-party.md) for their separate terms.

## Copyright

Copyright (c) 2026 sxingyao808-glitch
