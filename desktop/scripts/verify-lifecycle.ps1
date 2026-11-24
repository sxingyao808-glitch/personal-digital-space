#requires -Version 5.1
<#
.SYNOPSIS
  Lifecycle checks for the desktop shell, all inside one process so the app is
  not reaped between shell commands.

.DESCRIPTION
  1. one backend for one shell
  2. a second launch exits without starting a second backend
  3. force-killing the shell makes the backend exit by itself
  4. the same data directory opens again after a forced kill
  5. a normal window close stops the shell and the backend

.PARAMETER Exe
  PersonalDigitalSpace.exe (packaged) or electron.exe (development).

.PARAMETER AppDir
  Desktop directory used as the app path in development mode.

.PARAMETER DataDir
  Absolute data directory. Must not be the real archive for destructive runs.

.PARAMETER ProfileDir
  Isolated Chromium profile directory.

.PARAMETER ProcessImage
  Backend image name: python.exe (development) or archive-backend.exe.

.PARAMETER Packaged
  Omit the app path argument.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Exe,
  [Parameter(Mandatory = $true)][string]$AppDir,
  [Parameter(Mandatory = $true)][string]$DataDir,
  [Parameter(Mandatory = $true)][string]$ProfileDir,
  [string]$ProcessImage = 'python.exe',
  [switch]$Packaged
)

$ErrorActionPreference = 'Stop'
$script:failures = 0

function Report([string]$Name, [bool]$Ok, [string]$Detail) {
  $state = if ($Ok) { 'PASS' } else { 'FAIL' }
  if (-not $Ok) { $script:failures += 1 }
  Write-Host ("[{0}] {1} {2}" -f $state, $Name, $Detail)
}

function Get-BackendPids {
  $safeDataDir = $DataDir.Replace("'", "''")
  @(Get-CimInstance Win32_Process -Filter "Name='$ProcessImage'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($safeDataDir) } |
    Select-Object -ExpandProperty ProcessId)
}

function Start-Shell([int]$Port) {
  # Start-Process joins -ArgumentList with spaces and does not quote entries,
  # so every path-bearing argument is quoted here explicitly.
  $arguments = ''
  if (-not $Packaged) { $arguments += "`"$AppDir`" " }
  $arguments += "`"--user-data-dir=$ProfileDir`" `"--remote-debugging-port=$Port`""
  $env:ARCHIVE_DATA_DIR = $DataDir
  Start-Process -FilePath $Exe -ArgumentList $arguments -WorkingDirectory $AppDir -PassThru
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (& $Condition) { return $true }
    Start-Sleep -Milliseconds 400
  }
  return $false
}

function Test-Alive([int]$ProcessId) {
  return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
if (-not (Test-Path -LiteralPath $ProfileDir)) { New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null }

Write-Host "lifecycle: exe=$Exe image=$ProcessImage data=$DataDir"

# --- 1. first launch -------------------------------------------------------
$first = Start-Shell 9441
$started = Wait-Until { (Get-BackendPids).Count -eq 1 } 60
Report 'first.backend_started' $started ("pids=" + ((Get-BackendPids) -join ','))
if (-not $started) {
  try { $first | Stop-Process -Force } catch { }
  exit 1
}
$backendPid = (Get-BackendPids)[0]
Start-Sleep -Seconds 3
Report 'first.single_backend' ((Get-BackendPids).Count -eq 1) ("pid=" + $backendPid)

# --- 2. second launch ------------------------------------------------------
$second = Start-Shell 9442
$secondExited = Wait-Until { $second.HasExited } 25
$second.Refresh()
Report 'second.exited' $secondExited ("exit=" + $(if ($second.HasExited) { $second.ExitCode } else { 'running' }))
Start-Sleep -Seconds 2
$afterSecond = Get-BackendPids
Report 'second.no_extra_backend' ($afterSecond.Count -eq 1) ("pids=" + ($afterSecond -join ','))
Report 'second.same_backend' ($afterSecond.Count -eq 1 -and $afterSecond[0] -eq $backendPid) ("pids=" + ($afterSecond -join ','))
if (-not $second.HasExited) { try { $second | Stop-Process -Force } catch { } }

# --- 3. forced shell kill --------------------------------------------------
Stop-Process -Id $first.Id -Force -ErrorAction SilentlyContinue
$backendGone = Wait-Until { (Get-BackendPids).Count -eq 0 } 25
Report 'forced_kill.backend_exited' $backendGone ("remaining=" + ((Get-BackendPids) -join ','))
Start-Sleep -Seconds 1

# --- 4. restart on the same data directory ---------------------------------
$restarted = Start-Shell 9443
$restartOk = Wait-Until { (Get-BackendPids).Count -eq 1 } 60
Report 'restart.backend_started' $restartOk ("pids=" + ((Get-BackendPids) -join ','))
Report 'restart.database_present' (Test-Path -LiteralPath (Join-Path $DataDir 'archive.db'))
$restartedPid = @(Get-BackendPids)
if ($restartOk) { $restartedPid = (Get-BackendPids)[0] }
Start-Sleep -Seconds 3

# --- 5. normal close -------------------------------------------------------
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ShellWindow {
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}
"@
$closed = $false
$restarted.Refresh()
if ($restarted.MainWindowHandle -ne [IntPtr]::Zero) {
  # WM_CLOSE: identical to the user clicking the window close button.
  [void][ShellWindow]::PostMessage($restarted.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
  $closed = Wait-Until { $restarted.HasExited } 25
} else {
  $closed = $restarted.CloseMainWindow()
  if ($closed) { $closed = Wait-Until { $restarted.HasExited } 25 }
}
Report 'close.shell_exited' $closed ("exit=" + $(if ($restarted.HasExited) { $restarted.ExitCode } else { 'running' }))
$backendStopped = Wait-Until { -not (Test-Alive $restartedPid) } 25
Report 'close.backend_stopped' $backendStopped ("pid=" + $restartedPid)

if (-not $restarted.HasExited) { try { $restarted | Stop-Process -Force } catch { } }

if ($script:failures -gt 0) {
  Write-Host ("lifecycle failures: {0}" -f $script:failures)
  exit 1
}
Write-Host 'lifecycle: all checks passed'
exit 0
