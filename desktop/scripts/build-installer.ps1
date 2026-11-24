#requires -Version 5.1
<#
.SYNOPSIS
  Build the Windows sidecar and the NSIS installer.

.DESCRIPTION
  Steps: PyInstaller sidecar -> electron-builder (NSIS, x64).
  The default upstream download configuration is used. The installer is
  unsigned and may prompt Windows SmartScreen.

  Output: desktop/release/PersonalDigitalSpace-Setup-<version>.exe
#>
[CmdletBinding()]
param(
  [switch]$SkipBackend
)

$ErrorActionPreference = 'Stop'

$Desktop = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Desktop 'backend-dist\archive-backend'
$Exe = Join-Path $Dist 'archive-backend.exe'

if (-not $SkipBackend) {
  & (Join-Path $PSScriptRoot 'build-backend.ps1')
  if ($LASTEXITCODE -ne 0) { throw "backend build failed ($LASTEXITCODE)" }
}

if (-not (Test-Path -LiteralPath $Exe)) {
  throw "sidecar missing: $Exe (run build-backend.ps1)"
}

$env:CSC_IDENTITY_AUTO_DISCOVERY = 'false'

Push-Location $Desktop
try {
  npm.cmd run dist
  if ($LASTEXITCODE -ne 0) { throw "electron-builder failed ($LASTEXITCODE)" }
} finally {
  Pop-Location
}

$artifact = Get-ChildItem -LiteralPath (Join-Path $Desktop 'release') -Filter 'PersonalDigitalSpace-Setup-*.exe' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if (-not $artifact) { throw 'installer artifact not found' }

Write-Host ("installer: {0} ({1:N1} MB)" -f $artifact.FullName, ($artifact.Length / 1MB))
