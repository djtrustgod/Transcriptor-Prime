<#
.SYNOPSIS
    Draws the Transcriptor Prime logo and packs it into a multi-resolution .ico.

.DESCRIPTION
    The generated .ico is committed to the repository, so this script only needs
    running when the artwork changes:

        pwsh -File tools\make_icon.ps1

    Entries up to 64 px are uncompressed 32-bit BMP/DIB; 128 and 256 are
    PNG-compressed. That split is the long-standing Windows convention: every
    shell surface reads BMP entries, while PNG keeps the two large entries from
    dominating the file (all-BMP would be ~370 KB against ~60 KB).

    Each size is drawn at its own resolution rather than downscaled from one
    master, so the small entries stay crisp.

    The artwork is a waveform over two "text" lines -- audio becoming a
    transcript. Below 32 px the text lines are dropped and the bar count is
    reduced, because at 16 px the detail turns to mush.
#>
[CmdletBinding()]
param(
    [string]$OutputDir = (Join-Path $PSScriptRoot "..\src\transcriptor_prime\assets")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# Deep teal to navy. Saturated enough to stay legible on both light and dark
# taskbars, and distinct from the blues that dominate the Windows tray.
$ColorTopLeft     = [System.Drawing.Color]::FromArgb(255, 20, 184, 166)
$ColorBottomRight = [System.Drawing.Color]::FromArgb(255, 12, 74, 110)

function New-LogoBitmap {
    param([int]$Size)

    $bmp = New-Object System.Drawing.Bitmap($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    # --- rounded-square background -------------------------------------
    $radius = [double]$Size * 0.22
    $inset = [double]$Size * 0.02          # keeps antialiased edges off the bounds
    $x = $inset; $y = $inset
    $w = [double]$Size - (2 * $inset); $h = $w
    $d = $radius * 2

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    if ($d -ge 2) {
        $path.AddArc($x, $y, $d, $d, 180, 90)
        $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
        $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
        $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
        $path.CloseFigure()
    } else {
        $path.AddRectangle((New-Object System.Drawing.RectangleF($x, $y, $w, $h)))
    }

    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.PointF(0, 0)),
        (New-Object System.Drawing.PointF([single]$Size, [single]$Size)),
        $ColorTopLeft, $ColorBottomRight)
    $g.FillPath($brush, $path)
    $brush.Dispose()
    $path.Dispose()

    # --- foreground ------------------------------------------------------
    $detailed = $Size -ge 32
    $white = [System.Drawing.Color]::FromArgb(255, 255, 255, 255)

    # Symmetric bar heights, as a fraction of the icon. Fewer, fatter bars at
    # small sizes so each one still lands on a whole pixel.
    $heights = if ($detailed) { @(0.20, 0.38, 0.58, 0.74, 0.58, 0.38, 0.20) }
               else           { @(0.30, 0.58, 0.78, 0.58, 0.30) }

    $barWidth = if ($detailed) { [double]$Size * 0.072 } else { [double]$Size * 0.11 }
    $gap      = if ($detailed) { [double]$Size * 0.056 } else { [double]$Size * 0.075 }
    $waveCentreY = if ($detailed) { [double]$Size * 0.43 } else { [double]$Size * 0.50 }
    $heightScale = if ($detailed) { 0.62 } else { 0.70 }

    $count = $heights.Count
    $totalWidth = ($count * $barWidth) + (($count - 1) * $gap)
    $startX = ([double]$Size - $totalWidth) / 2.0 + ($barWidth / 2.0)

    $pen = New-Object System.Drawing.Pen($white, [single]$barWidth)
    if ($barWidth -ge 3) {
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    }

    for ($i = 0; $i -lt $count; $i++) {
        $cx = $startX + ($i * ($barWidth + $gap))
        $half = ([double]$Size * $heights[$i] * $heightScale) / 2.0
        # Round caps add half a pen width at each end; pull the line in so the
        # painted bar matches the intended height.
        if ($barWidth -ge 3) { $half = [Math]::Max($barWidth / 2.0, $half - ($barWidth / 2.0)) }
        $g.DrawLine($pen, [single]$cx, [single]($waveCentreY - $half), [single]$cx, [single]($waveCentreY + $half))
    }
    $pen.Dispose()

    if ($detailed) {
        # Two "lines of transcript" under the waveform.
        $lineHeight = [double]$Size * 0.055
        $linePen = New-Object System.Drawing.Pen($white, [single]$lineHeight)
        $linePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $linePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round

        foreach ($line in @(@{ y = 0.76; w = 0.46 }, @{ y = 0.885; w = 0.30 })) {
            $ly = [double]$Size * $line.y
            $lw = [double]$Size * $line.w
            $lx = ([double]$Size - $lw) / 2.0
            $g.DrawLine($linePen, [single]$lx, [single]$ly, [single]($lx + $lw), [single]$ly)
        }
        $linePen.Dispose()
    }

    $g.Dispose()
    return $bmp
}

function Get-DibEntry {
    <# One ICO entry: BITMAPINFOHEADER + bottom-up BGRA pixels + empty AND mask. #>
    param([System.Drawing.Bitmap]$Bitmap)

    $width = $Bitmap.Width; $height = $Bitmap.Height
    $rect = New-Object System.Drawing.Rectangle(0, 0, $width, $height)
    $data = $Bitmap.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
                             [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $stride = $data.Stride
        $raw = New-Object byte[] ($stride * $height)
        [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $raw, 0, $raw.Length)
    } finally {
        $Bitmap.UnlockBits($data)
    }

    $stream = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($stream)

    # BITMAPINFOHEADER. Height is doubled: the DIB notionally holds the colour
    # bitmap stacked on top of the AND mask.
    $writer.Write([uint32]40)
    $writer.Write([int32]$width)
    $writer.Write([int32]($height * 2))
    $writer.Write([uint16]1)
    $writer.Write([uint16]32)
    $writer.Write([uint32]0)                       # BI_RGB
    $writer.Write([uint32]($width * $height * 4))
    $writer.Write([int32]0); $writer.Write([int32]0)
    $writer.Write([uint32]0); $writer.Write([uint32]0)

    # Colour data, bottom-up.
    for ($row = $height - 1; $row -ge 0; $row--) {
        $writer.Write($raw, $row * $stride, $width * 4)
    }

    # AND mask: all zero. 32-bit icons key off the alpha channel, but the mask
    # still has to be present and row-padded to 4 bytes.
    $maskStride = [int][Math]::Floor((($width + 31) / 32)) * 4
    $writer.Write((New-Object byte[] ($maskStride * $height)))

    $writer.Flush()
    $bytes = $stream.ToArray()
    $writer.Dispose(); $stream.Dispose()
    # The leading comma stops PowerShell unrolling the array into the pipeline,
    # which would hand the caller loose bytes instead of a byte[].
    return , $bytes
}

function Get-PngEntry {
    <# One ICO entry stored as a PNG stream. Vista and later only. #>
    param([System.Drawing.Bitmap]$Bitmap)

    $stream = New-Object System.IO.MemoryStream
    $Bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose()
    return , $bytes
}

# 256 keeps Explorer's largest views crisp; 20/24/40 cover the intermediate
# sizes Windows picks at 125-200% display scaling.
$sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256)
$PngFrom = 128            # sizes at or above this are PNG-compressed
$entries = @()
foreach ($size in $sizes) {
    $bmp = New-LogoBitmap -Size $size
    $data = if ($size -ge $PngFrom) { Get-PngEntry -Bitmap $bmp } else { Get-DibEntry -Bitmap $bmp }
    $entries += [pscustomobject]@{ Size = $size; Data = $data }
    if ($size -eq 256) { $bmp.Save((Join-Path $OutputDir "logo.png"), [System.Drawing.Imaging.ImageFormat]::Png) }
    $bmp.Dispose()
}

$icoPath = Join-Path $OutputDir "transcriptor-prime.ico"
$out = New-Object System.IO.FileStream($icoPath, [System.IO.FileMode]::Create)
$bw = New-Object System.IO.BinaryWriter($out)

$bw.Write([uint16]0)                    # reserved
$bw.Write([uint16]1)                    # type: icon
$bw.Write([uint16]$entries.Count)

$offset = 6 + (16 * $entries.Count)
foreach ($entry in $entries) {
    $dim = if ($entry.Size -ge 256) { 0 } else { $entry.Size }   # 0 encodes 256
    $bw.Write([byte]$dim)               # width
    $bw.Write([byte]$dim)               # height
    $bw.Write([byte]0)                  # palette size
    $bw.Write([byte]0)                  # reserved
    $bw.Write([uint16]1)                # colour planes
    $bw.Write([uint16]32)               # bits per pixel
    $bw.Write([uint32]$entry.Data.Length)
    $bw.Write([uint32]$offset)
    $offset += $entry.Data.Length
}
foreach ($entry in $entries) { $bw.Write([byte[]]$entry.Data, 0, $entry.Data.Length) }

$bw.Flush(); $bw.Dispose(); $out.Dispose()

$kb = [Math]::Round((Get-Item $icoPath).Length / 1KB)
Write-Output "wrote $icoPath ($($entries.Count) sizes, $kb KB)"
Write-Output "wrote $(Join-Path $OutputDir 'logo.png')"
