#requires -Version 5.1
<#
.SYNOPSIS
  Generate the desktop application icon (multi-size ICO + 256px PNG) and the
  website favicon (16/32/48 ICO) from build/icon-source.png.

.DESCRIPTION
  The source artwork is a square app tile centred on a black canvas. The script
  detects the tile bounds, crops to them, scales each size separately with
  high-quality bicubic sampling and applies an anti-aliased rounded-rectangle
  alpha mask so the icon has transparent corners.

  Outputs:
    build/icon.ico                  - electron-builder, NSIS and window icon
    build/icon.png                  - 256px preview
    archive/web/static/favicon.ico  - website favicon (16/32/48)

  This file is intentionally ASCII-only: Windows PowerShell 5.1 decodes
  BOM-less script files as ANSI, which can corrupt non-ASCII source.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourcePath = Join-Path $BuildDir 'icon-source.png'
$IcoPath = Join-Path $BuildDir 'icon.ico'
$PngPath = Join-Path $BuildDir 'icon.png'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $BuildDir)
$FaviconPath = Join-Path $RepoRoot 'archive\web\static\favicon.ico'

if (-not (Test-Path -LiteralPath $SourcePath)) {
  throw "icon source missing: $SourcePath"
}

# Corner radius of the source tile as a share of its side length.
$RadiusRatio = 0.225

function Get-TileBounds([System.Drawing.Bitmap]$Bitmap) {
  $scan = 256
  $preview = New-Object System.Drawing.Bitmap($scan, $scan, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $graphics = [System.Drawing.Graphics]::FromImage($preview)
  $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $graphics.DrawImage($Bitmap, 0, 0, $scan, $scan)
  $graphics.Dispose()

  $minX = $scan; $minY = $scan; $maxX = -1; $maxY = -1
  for ($y = 0; $y -lt $scan; $y++) {
    for ($x = 0; $x -lt $scan; $x++) {
      $c = $preview.GetPixel($x, $y)
      $lum = [Math]::Max($c.R, [Math]::Max($c.G, $c.B))
      if ($lum -gt 16) {
        if ($x -lt $minX) { $minX = $x }
        if ($x -gt $maxX) { $maxX = $x }
        if ($y -lt $minY) { $minY = $y }
        if ($y -gt $maxY) { $maxY = $y }
      }
    }
  }
  $preview.Dispose()

  if ($maxX -lt 0) { throw 'icon source appears to be fully black' }

  $sx = $Bitmap.Width / [single]$scan
  $sy = $Bitmap.Height / [single]$scan
  $left = [int][Math]::Floor(($minX - 1) * $sx)
  $top = [int][Math]::Floor(($minY - 1) * $sy)
  $right = [int][Math]::Ceiling(($maxX + 2) * $sx)
  $bottom = [int][Math]::Ceiling(($maxY + 2) * $sy)

  $left = [Math]::Max(0, $left)
  $top = [Math]::Max(0, $top)
  $right = [Math]::Min($Bitmap.Width, $right)
  $bottom = [Math]::Min($Bitmap.Height, $bottom)

  # Keep the crop square and inside the canvas.
  $side = [Math]::Max($right - $left, $bottom - $top)
  $cx = ($left + $right) / 2
  $cy = ($top + $bottom) / 2
  $left = [int][Math]::Round($cx - ($side / 2))
  $top = [int][Math]::Round($cy - ($side / 2))
  if ($left -lt 0) { $left = 0 }
  if ($top -lt 0) { $top = 0 }
  if (($left + $side) -gt $Bitmap.Width) { $side = $Bitmap.Width - $left }
  if (($top + $side) -gt $Bitmap.Height) { $side = $Bitmap.Height - $top }

  return New-Object System.Drawing.Rectangle($left, $top, $side, $side)
}

function Set-RoundedAlpha([System.Drawing.Bitmap]$Bitmap, [single]$Radius) {
  $width = $Bitmap.Width
  $height = $Bitmap.Height
  $rect = New-Object System.Drawing.Rectangle(0, 0, $width, $height)
  $data = $Bitmap.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadWrite, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  try {
    $stride = $data.Stride
    $buffer = New-Object byte[] ($stride * $height)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $buffer, 0, $buffer.Length)

    $halfW = $width / 2.0
    $halfH = $height / 2.0
    $innerX = $halfW - $Radius
    $innerY = $halfH - $Radius

    for ($y = 0; $y -lt $height; $y++) {
      $rowStart = $y * $stride
      $dy = [Math]::Abs(($y + 0.5) - $halfH) - $innerY
      for ($x = 0; $x -lt $width; $x++) {
        $dx = [Math]::Abs(($x + 0.5) - $halfW) - $innerX
        $outsideX = if ($dx -gt 0) { $dx } else { 0 }
        $outsideY = if ($dy -gt 0) { $dy } else { 0 }
        $maxCorner = if ($dx -gt $dy) { $dx } else { $dy }
        $insideOffset = if ($maxCorner -lt 0) { $maxCorner } else { 0 }
        $distance = [Math]::Sqrt(($outsideX * $outsideX) + ($outsideY * $outsideY)) + $insideOffset - $Radius

        $coverage = 0.5 - $distance
        if ($coverage -le 0) { $coverage = 0 }
        elseif ($coverage -ge 1) { $coverage = 1 }

        $offset = $rowStart + ($x * 4) + 3
        if ($coverage -lt 1) {
          $buffer[$offset] = [byte][Math]::Round($buffer[$offset] * $coverage)
        }
      }
    }

    [System.Runtime.InteropServices.Marshal]::Copy($buffer, 0, $data.Scan0, $buffer.Length)
  } finally {
    $Bitmap.UnlockBits($data)
  }
}

function New-IconPng([System.Drawing.Bitmap]$Source, [System.Drawing.Rectangle]$Crop, [int]$Size) {
  $bitmap = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
  $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
  $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  $graphics.Clear([System.Drawing.Color]::Transparent)
  $graphics.DrawImage(
    $Source,
    (New-Object System.Drawing.Rectangle(0, 0, $Size, $Size)),
    $Crop,
    [System.Drawing.GraphicsUnit]::Pixel
  )
  $graphics.Dispose()

  Set-RoundedAlpha -Bitmap $bitmap -Radius ([single]($Size * $RadiusRatio))

  $buffer = New-Object System.IO.MemoryStream
  $bitmap.Save($buffer, [System.Drawing.Imaging.ImageFormat]::Png)
  $bytes = $buffer.ToArray()
  $buffer.Dispose()
  $bitmap.Dispose()

  return ,$bytes
}

$source = [System.Drawing.Bitmap]::FromFile($SourcePath)
try {
  $crop = Get-TileBounds -Bitmap $source

  $sizes = @(16, 24, 32, 48, 64, 128, 256)
  $images = @()
  foreach ($size in $sizes) {
    $images += [pscustomobject]@{ Size = $size; Bytes = (New-IconPng -Source $source -Crop $crop -Size $size) }
  }
} finally {
  $source.Dispose()
}

function Write-Ico([object[]]$Images, [string]$Path) {
  # ICO layout: header + one directory entry per image + PNG payloads.
  $stream = New-Object System.IO.MemoryStream
  $writer = New-Object System.IO.BinaryWriter($stream)
  $writer.Write([UInt16]0)
  $writer.Write([UInt16]1)
  $writer.Write([UInt16]$Images.Count)

  $offset = 6 + (16 * $Images.Count)
  foreach ($image in $Images) {
    $dimension = if ($image.Size -ge 256) { 0 } else { $image.Size }
    $writer.Write([byte]$dimension)
    $writer.Write([byte]$dimension)
    $writer.Write([byte]0)
    $writer.Write([byte]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]32)
    $writer.Write([UInt32]$image.Bytes.Length)
    $writer.Write([UInt32]$offset)
    $offset += $image.Bytes.Length
  }
  foreach ($image in $Images) {
    $writer.Write($image.Bytes)
  }
  $writer.Flush()
  [System.IO.File]::WriteAllBytes($Path, $stream.ToArray())
  $writer.Dispose()
  $stream.Dispose()
}

$preview = $images | Where-Object { $_.Size -eq 256 } | Select-Object -First 1
[System.IO.File]::WriteAllBytes($PngPath, $preview.Bytes)

Write-Ico -Images $images -Path $IcoPath
$faviconImages = @($images | Where-Object { $_.Size -in @(16, 32, 48) })
Write-Ico -Images $faviconImages -Path $FaviconPath

Write-Host "icon: $IcoPath ($((Get-Item $IcoPath).Length) bytes)"
Write-Host "png:  $PngPath ($((Get-Item $PngPath).Length) bytes)"
Write-Host "favicon: $FaviconPath ($((Get-Item $FaviconPath).Length) bytes)"
Write-Host "crop: $($crop.X),$($crop.Y) $($crop.Width)x$($crop.Height)"
