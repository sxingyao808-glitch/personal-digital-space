#requires -Version 5.1
<#
.SYNOPSIS
  Build the Python sidecar with PyInstaller (one-directory, console subsystem).

.DESCRIPTION
  Uses the separately prepared CPython and SQLite runtime in runtime-lock.json.
  Output: desktop/backend-dist/archive-backend/archive-backend.exe
#>
[CmdletBinding()]
param(
  [switch]$SkipInstall,
  [string]$BuildPython = $env:PDS_BUILD_PYTHON
)

$ErrorActionPreference = 'Stop'

$Desktop = Split-Path -Parent $PSScriptRoot
$Root = Split-Path -Parent $Desktop
$Requirements = Join-Path $Root 'requirements.txt'
$Spec = Join-Path $Desktop 'backend\archive-backend.spec'
$Dist = Join-Path $Desktop 'backend-dist'
$Work = Join-Path $Desktop 'backend-build'

if (-not $BuildPython) {
  throw 'Set PDS_BUILD_PYTHON to the verified Windows x64 build runtime in desktop/runtime-lock.json.'
}
$Python = (Resolve-Path -LiteralPath $BuildPython).Path
$runtime = Get-Content -LiteralPath (Join-Path $Desktop 'runtime-lock.json') -Raw | ConvertFrom-Json
$expected = $runtime.sqlite_source_id
$pythonVersion = & $Python -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne $runtime.python_version) { throw 'Build-time CPython differs from lock.' }
$loaded = & $Python -c "import sqlite3; print(sqlite3.connect(':memory:').execute('select sqlite_source_id()').fetchone()[0])"
if ($LASTEXITCODE -ne 0 -or $loaded -ne $expected) { throw 'Build-time SQLite runtime differs from lock.' }

if (-not $SkipInstall) {
  Write-Host "installing backend dependencies"
  & $Python -m pip install --disable-pip-version-check --quiet -r $Requirements 'pyinstaller==6.22.3'
  if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }
}

Write-Host "building sidecar"
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$Exe = Join-Path $Dist 'archive-backend\archive-backend.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw "sidecar not found: $Exe" }

$sqliteDll = Join-Path $Dist 'archive-backend/_internal/sqlite3.dll'
if ((Get-FileHash -LiteralPath $sqliteDll -Algorithm SHA256).Hash -ne $runtime.sqlite_dll_sha256) {
  throw 'Packaged SQLite DLL differs from lock.'
}
$folder = Get-ChildItem -LiteralPath (Join-Path $Dist 'archive-backend') -Recurse -File |
  Measure-Object -Property Length -Sum
Write-Host ("sidecar: {0} ({1:N1} MB, {2} files)" -f $Exe, ($folder.Sum / 1MB), $folder.Count)
