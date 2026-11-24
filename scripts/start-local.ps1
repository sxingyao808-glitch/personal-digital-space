#requires -Version 5.1
<#
.SYNOPSIS
  启动 Archive 本地服务（固定端口 8765，仅监听 127.0.0.1）。

.DESCRIPTION
  - 数据目录必须显式指定或使用本机默认目录，不依赖当前工作目录。
  - 日志写入数据目录之外的本地日志目录，并对令牌做掩码，不写入持久日志。
  - 不把长期 Owner 令牌硬编码在脚本里；登录仍使用既有 bootstrap token 机制。
  - 同一数据目录的第二实例会被单实例锁拒绝（退出码 3）。

.PARAMETER DataDir
  数据目录。省略时使用 $env:LOCALAPPDATA\PersonalDigitalSpaceOSS\source-data。

.PARAMETER LogDir
  日志目录。省略时使用 $env:LOCALAPPDATA\PersonalDigitalSpaceOSS\logs。

.PARAMETER Port
  本地端口，默认固定 8765。

.PARAMETER AllowInit
  允许在一个已存在但缺少 archive.db 的目录中初始化新数据库。

.PARAMETER Python
  Python 可执行文件，默认 python。
#>
[CmdletBinding()]
param(
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'PersonalDigitalSpaceOSS\source-data'),
  [string]$LogDir = (Join-Path $env:LOCALAPPDATA 'PersonalDigitalSpaceOSS\logs'),
  [int]$Port = 8765,
  [switch]$AllowInit,
  [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunApp = Join-Path $RepoRoot 'run_app.py'

function Mask-Line([string]$Line) {
  # Login URLs and token echoes must never reach a persistent log file.
  $masked = $Line
  $masked = [regex]::Replace($masked, 'token=[A-Za-z0-9_\-\.]+', 'token=<redacted>')
  $masked = [regex]::Replace($masked, '(令牌\s*[:：]\s*)\S+', '$1<redacted>')
  $masked = [regex]::Replace($masked, '(bootstrap_token\s*[=:]\s*)\S+', '$1<redacted>')
  # Safety net that does not depend on the surrounding language/encoding:
  # redact long base64url-looking tokens that end a line.
  $masked = [regex]::Replace($masked, '(?<=[\s:：])([A-Za-z0-9_\-]{24,})(?=\s*$)', '<redacted>')
  return $masked
}

function Write-Log([string]$Line) {
  if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
  }
  $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  $masked = Mask-Line $Line
  Add-Content -LiteralPath $script:LogFile -Value "[$stamp] $masked" -Encoding utf8
}

if (-not (Test-Path -LiteralPath $RunApp)) {
  throw "未找到 run_app.py：$RunApp"
}

$DataDir = [System.IO.Path]::GetFullPath($DataDir)
$LogDir = [System.IO.Path]::GetFullPath($LogDir)
$DataDb = Join-Path $DataDir 'archive.db'

# --- directory safety: never silently initialise an unexpected directory ----
if (Test-Path -LiteralPath $DataDir) {
  $existing = @(Get-ChildItem -LiteralPath $DataDir -Force -ErrorAction SilentlyContinue)
  if ($existing.Count -gt 0 -and -not (Test-Path -LiteralPath $DataDb) -and -not $AllowInit) {
    throw ("数据目录已存在但缺少 archive.db：$DataDir`n" +
      "它可能不是本应用的数据目录，直接启动会新建空数据库，容易让人误以为旧数据丢失。`n" +
      "请确认目录正确；确实要在该目录初始化新库时加 -AllowInit。")
  }
} else {
  New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$script:LogFile = Join-Path $LogDir ("start-local-" + (Get-Date).ToString('yyyyMMdd') + ".log")
Write-Log "启动请求：data-dir=$DataDir port=$Port"

if ($Port -ne 8765) {
  Write-Host "注意：本脚本默认固定端口 8765，本次显式使用 $Port。" -ForegroundColor Yellow
}

# --- launch the service with filtered, token-free logging -------------------
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = "`"$RunApp`" --data-dir `"$DataDir`" --port $Port --no-open"
$psi.WorkingDirectory = $RepoRoot
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
$psi.EnvironmentVariables['PYTHONUTF8'] = '1'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
$proc.EnableRaisingEvents = $true

$onOut = {
  param($sender, $eventArgs)
  if ($null -ne $eventArgs.Data) {
    Write-Host $eventArgs.Data
    Write-Log $eventArgs.Data
  }
}
$onErr = {
  param($sender, $eventArgs)
  if ($null -ne $eventArgs.Data) {
    Write-Host $eventArgs.Data -ForegroundColor Yellow
    Write-Log ("stderr: " + $eventArgs.Data)
  }
}

$handlerOut = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $onOut
$handlerErr = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $onErr

try {
  [void]$proc.Start()
  $proc.BeginOutputReadLine()
  $proc.BeginErrorReadLine()

  Write-Host ''
  Write-Host 'Archive 本地服务启动中（仅监听 127.0.0.1）' -ForegroundColor Cyan
  Write-Host "  数据目录: $DataDir"
  Write-Host "  监听地址: http://127.0.0.1:$Port/"
  Write-Host "  日志文件: $script:LogFile（令牌已掩码）"
  Write-Host '  登录地址与服务启动输出显示在本控制台；如浏览器会话失效，请重新运行本脚本取得新的登录地址。' -ForegroundColor DarkGray
  Write-Host '  按 Ctrl+C 停止服务。' -ForegroundColor DarkGray
  Write-Host ''

  while (-not $proc.HasExited) {
    Start-Sleep -Milliseconds 400
  }
  $exit = $proc.ExitCode
  Write-Log "进程退出：exit=$exit"
  if ($exit -eq 3) {
    Write-Host '数据目录已被另一个运行中的实例占用，本次启动已停止。' -ForegroundColor Red
    Write-Host '请先正常退出该实例；不要手工删除 .app.lock。' -ForegroundColor Red
  } elseif ($exit -eq 2) {
    Write-Host '数据库 schema 不兼容，服务已停止。' -ForegroundColor Red
  }
  exit $exit
}
finally {
  Unregister-Event -SourceIdentifier $handlerOut.Name -ErrorAction SilentlyContinue
  Unregister-Event -SourceIdentifier $handlerErr.Name -ErrorAction SilentlyContinue
  Remove-Job -Name $handlerOut.Name -Force -ErrorAction SilentlyContinue
  Remove-Job -Name $handlerErr.Name -Force -ErrorAction SilentlyContinue
  if (-not $proc.HasExited) {
    Write-Host '正在停止服务（数据已提交，单实例锁由操作系统释放）…' -ForegroundColor DarkGray
    Write-Log '收到中断，终止子进程'
    try { $proc.Kill($true) } catch { }
    $proc.WaitForExit(5000) | Out-Null
  }
}
