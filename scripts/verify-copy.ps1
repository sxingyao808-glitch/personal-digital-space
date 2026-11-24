#requires -Version 5.1
<#
.SYNOPSIS
  校验冷备份副本与源数据目录是否一致（只读）。

.DESCRIPTION
  - 比较相对路径集合、文件大小与 SHA-256。
  - 分别报告缺失、多余、大小不一致与哈希不一致。
  - 不修改源目录或备份目录；默认输出 JSON，可用 -Report 另存。
  - 若备份目录含 manifest.json，会先核对清单自身记录的文件与哈希，再与源目录比较。

.PARAMETER Source
  源数据目录（应处于停服状态）。

.PARAMETER Copy
  备份目录。

.PARAMETER Report
  可选：把 JSON 结果写入该路径（不覆盖已有文件）。

.EXITCODES
  0 = 完全一致；1 = 存在差异；2 = 参数或环境错误。
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Source,
  [Parameter(Mandatory = $true)][string]$CopyDir,
  [string]$Report
)

$ErrorActionPreference = 'Stop'
$Source = [System.IO.Path]::GetFullPath($Source)
$CopyDir = [System.IO.Path]::GetFullPath($CopyDir)

function EmitResult($Result, [int]$Code) {
  $json = ($Result | ConvertTo-Json -Depth 6)
  if ($Report) {
    if (Test-Path -LiteralPath $Report) {
      Write-Host "拒绝覆盖已有报告：$Report" -ForegroundColor Red
    } else {
      [System.IO.File]::WriteAllText(
        $Report,
        $json,
        (New-Object System.Text.UTF8Encoding($false))
      )
    }
  }
  Write-Output $json
  if ($Code -eq 0) {
    Write-Host "校验通过：$Source == $CopyDir" -ForegroundColor Green
  } else {
    Write-Host "校验发现问题，详见 JSON 输出。" -ForegroundColor Red
  }
  exit $Code
}

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
  EmitResult @{ ok = $false; error = "源目录不存在：$Source" } 2
}
if (-not (Test-Path -LiteralPath $CopyDir -PathType Container)) {
  EmitResult @{ ok = $false; error = "备份目录不存在：$CopyDir" } 2
}

function Get-FileMap([string]$Root) {
  $map = @{}
  Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($Root.Length + 1).Replace('\', '/')
    $map[$relative] = $_
  }
  return $map
}

$sourceMap = Get-FileMap $Source
$copyMap = Get-FileMap $CopyDir
$copyMap.Remove('manifest.json') | Out-Null
$sourceMap.Remove('.app.lock') | Out-Null
$copyMap.Remove('.app.lock') | Out-Null

$missing = New-Object System.Collections.Generic.List[string]
$extra = New-Object System.Collections.Generic.List[string]
$sizeMismatch = New-Object System.Collections.Generic.List[object]
$hashMismatch = New-Object System.Collections.Generic.List[object]
$matched = 0

foreach ($relative in $sourceMap.Keys) {
  if (-not $copyMap.ContainsKey($relative)) {
    $missing.Add($relative)
    continue
  }
  $src = $sourceMap[$relative]
  $dst = $copyMap[$relative]
  if ($src.Length -ne $dst.Length) {
    $sizeMismatch.Add([ordered]@{ path = $relative; source_bytes = $src.Length; copy_bytes = $dst.Length })
    continue
  }
  $srcHash = (Get-FileHash -LiteralPath $src.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  $dstHash = (Get-FileHash -LiteralPath $dst.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($srcHash -ne $dstHash) {
    $hashMismatch.Add([ordered]@{ path = $relative; source_sha256 = $srcHash; copy_sha256 = $dstHash })
    continue
  }
  $matched++
}
foreach ($relative in $copyMap.Keys) {
  if (-not $sourceMap.ContainsKey($relative)) {
    $extra.Add($relative)
  }
}

# --- manifest self-consistency (when the backup carries one) ---------------
$manifestResult = $null
$manifestPath = Join-Path $CopyDir 'manifest.json'
if (Test-Path -LiteralPath $manifestPath) {
  $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
  $manifestIssues = New-Object System.Collections.Generic.List[object]
  foreach ($entry in @($manifest.files)) {
    $target = Join-Path $CopyDir ($entry.path.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $target)) {
      $manifestIssues.Add([ordered]@{ path = $entry.path; issue = 'missing_from_copy' })
      continue
    }
    if ((Get-Item -LiteralPath $target).Length -ne $entry.bytes) {
      $manifestIssues.Add([ordered]@{ path = $entry.path; issue = 'size_mismatch' })
      continue
    }
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne "$($entry.sha256)".ToLowerInvariant()) {
      $manifestIssues.Add([ordered]@{ path = $entry.path; issue = 'sha256_mismatch' })
    }
  }
  $manifestResult = [ordered]@{
    format          = $manifest.format
    format_version  = $manifest.format_version
    created_at      = $manifest.created_at
    program_version = $manifest.program_version
    declared_files  = @($manifest.files).Count
    issues          = $manifestIssues.ToArray()
  }
}

$ok = ($missing.Count + $extra.Count + $sizeMismatch.Count + $hashMismatch.Count -eq 0) -and
      ($null -eq $manifestResult -or @($manifestResult.issues).Count -eq 0)

EmitResult ([ordered]@{
  ok             = $ok
  source         = $Source
  copy           = $CopyDir
  checked_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  source_files   = $sourceMap.Count
  copy_files     = $copyMap.Count
  matched        = $matched
  missing        = $missing.ToArray()
  extra          = $extra.ToArray()
  size_mismatch  = $sizeMismatch.ToArray()
  hash_mismatch  = $hashMismatch.ToArray()
  manifest       = $manifestResult
}) $(if ($ok) { 0 } else { 1 })
