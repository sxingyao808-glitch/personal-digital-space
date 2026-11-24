#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$ArchiveRoot = Split-Path -Parent $PSScriptRoot
$ArchiveSource = [IO.Path]::GetFullPath($DataDir)
$ArchiveDestination = [IO.Path]::GetFullPath($BackupDir)
Push-Location -LiteralPath $ArchiveRoot
try {
    & $Python -B -m archive.cold_backup --data-dir $ArchiveSource --backup-dir $ArchiveDestination
    $ArchiveBackupCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ArchiveBackupCode
