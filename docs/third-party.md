# Third-party dependencies and assets

This repository contains source and dependency manifests. Third-party packages and runtime binaries are obtained separately. Their own license terms and notices apply.

| Dependency | Role | Upstream terms |
| --- | --- | --- |
| Flask | Python web service | BSD-3-Clause |
| Python blake3 bindings | Content hashing | CC0-1.0 or Apache-2.0 |
| CPython | Runtime and Windows sidecar build | Python Software Foundation License and bundled notices |
| SQLite | Archive database | Public domain dedication |
| Electron | Desktop runtime | MIT, with separate Chromium and bundled component notices |
| electron-builder | Windows packaging | MIT, with separate terms for downloaded build tools |
| PyInstaller | Build-time sidecar tool | GPL-2.0-or-later with a bootloader distribution exception |

See [requirements.txt](../requirements.txt) and [desktop/package-lock.json](../desktop/package-lock.json) for dependency versions. The npm lockfile records upstream license identifiers for its dependencies. For binary redistribution, review the exact packaged versions and include the applicable upstream license texts and notices, including those for Electron, Chromium, Python and the frozen Python dependencies.

The application and favicon artwork use original geometric artwork prepared for this source tree. The interface uses system font stacks.
