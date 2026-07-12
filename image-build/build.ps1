# RPI2DMD v3 image build — Windows wrapper.
#
# Copies the inputs into the WSL2 filesystem (~/rpi2dmd-build) for I/O
# speed, runs image-build/build.sh + verify-image.sh as root inside WSL,
# then copies the finished image back to Windows.
#
# Usage (from any PowerShell prompt):
#   .\build.ps1                 # full 7.0 GiB image with the 10K DLC
#   .\build.ps1 -Lite -SizeGb 3.7   # small image without the DLC packs
#
# Requirements: WSL2 with an Ubuntu (or similar) default distro providing
# sfdisk/losetup/dosfstools/e2fsprogs/rsync/curl/python3 and
# qemu-arm-static with binfmt_misc ARM registration.

[CmdletBinding()]
param(
    [string]$V2Img,
    [string]$Repo,
    [string]$Content,
    [string]$OutImg,
    [double]$SizeGb = 7.0,
    [switch]$Lite,
    [switch]$SkipCopy,      # reuse inputs already inside the WSL build dir
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'

# defaults derived from the standard layout: <base>\rpi2dmd-v3\image-build\build.ps1
$RepoDir = Split-Path $PSScriptRoot -Parent
$BaseDir = Split-Path $RepoDir -Parent
if (-not $Repo)    { $Repo = $RepoDir }
if (-not $V2Img)   { $V2Img = Join-Path $BaseDir 'RPI2DMD_v2_standard.img' }
if (-not $Content) { $Content = Join-Path $BaseDir 'v3-content' }
if (-not $OutImg)  { $OutImg = Join-Path $BaseDir 'RPI2DMD_v3.img' }

function ConvertTo-WslPath([string]$WinPath) {
    $p = (Resolve-Path $WinPath).Path
    $drive = $p.Substring(0, 1).ToLower()
    $rest = $p.Substring(2) -replace '\\', '/'
    return "/mnt/$drive$rest"
}

function Invoke-Wsl([string[]]$CommandArgs, [string]$What) {
    Write-Host ">> $What"
    # Windows PowerShell 5.1 wraps native stderr lines in ErrorRecords; with
    # ErrorActionPreference=Stop a mere warning on stderr would kill the
    # build mid-command. Relax it around the call and trust the exit code.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & wsl -u root -e @CommandArgs
    } finally {
        $ErrorActionPreference = $eap
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE)"
    }
}

foreach ($p in @($V2Img, $Repo, $Content)) {
    if (-not (Test-Path $p)) { throw "input not found: $p" }
}

$B = '/root/rpi2dmd-build'
$stamp = Get-Date
Write-Host "=== RPI2DMD v3 image build ==="
Write-Host "v2 image : $V2Img"
Write-Host "repo     : $Repo"
Write-Host "content  : $Content"
Write-Host "output   : $OutImg"
Write-Host ("mode     : size {0} GiB{1}" -f $SizeGb, $(if ($Lite) { ', LITE (no 10K DLC)' } else { '' }))

$repoW = ConvertTo-WslPath $Repo
$v2W = ConvertTo-WslPath $V2Img
$contentW = ConvertTo-WslPath $Content

if (-not $SkipCopy) {
    Invoke-Wsl @('mkdir', '-p', "$B/content") 'create WSL build dir'

    # repo (small; excludes dev noise — build.sh excludes tests/ itself)
    Invoke-Wsl @('rsync', '-a', '--delete',
        '--exclude', '.git', '--exclude', '__pycache__', '--exclude', '.pytest_cache',
        "$repoW/", "$B/repo/") 'copy repo into WSL'

    # v2 base image (~3.7 GB — takes a few minutes from the Windows drive)
    Invoke-Wsl @('rsync', '-t', '--inplace', $v2W,
        "$B/RPI2DMD_v2_standard.img") 'copy v2 base image into WSL'

    # content: dmd + media-base always; DLC packs only for full builds
    Invoke-Wsl @('rsync', '-a', '--delete', "$contentW/dmd/",
        "$B/content/dmd/") 'copy RDA library into WSL'
    Invoke-Wsl @('rsync', '-a', '--delete', "$contentW/media-base/",
        "$B/content/media-base/") 'copy media-base into WSL'
    if (-not $Lite) {
        Invoke-Wsl @('rsync', '-a', '--delete', "$contentW/dlc10k/",
            "$B/content/dlc10k/") 'copy 10K DLC into WSL (~1.5 GB)'
        Invoke-Wsl @('rsync', '-a', '--delete', "$contentW/bonus/",
            "$B/content/bonus/") 'copy bonus pack into WSL'
    }
}

# build (the chroot apt + rgbmatrix build under qemu can take 30-90+ min)
$buildArgs = @('bash', "$B/repo/image-build/build.sh",
    '--v2-img', "$B/RPI2DMD_v2_standard.img",
    '--repo', "$B/repo",
    '--content', "$B/content",
    '--out', "$B/RPI2DMD_v3.img",
    '--size-gb', $SizeGb.ToString([System.Globalization.CultureInfo]::InvariantCulture))
if ($Lite) { $buildArgs += '--lite' }
Invoke-Wsl $buildArgs 'build.sh'

if (-not $SkipVerify) {
    $verifyArgs = @('bash', "$B/repo/image-build/verify-image.sh",
        '--img', "$B/RPI2DMD_v3.img")
    if ($Lite) { $verifyArgs += '--lite' }
    Invoke-Wsl $verifyArgs 'verify-image.sh'
}

# copy the result back to Windows (materializes the sparse file)
$outDirW = ConvertTo-WslPath (Split-Path $OutImg -Parent)
$outName = Split-Path $OutImg -Leaf
Invoke-Wsl @('cp', "$B/RPI2DMD_v3.img", "$outDirW/$outName") 'copy image to Windows'
Invoke-Wsl @('cp', "$B/RPI2DMD_v3.img.build-info.txt",
    "$outDirW/$outName.build-info.txt") 'copy build info to Windows'

$mins = [math]::Round(((Get-Date) - $stamp).TotalMinutes, 1)
$sizeGbOut = [math]::Round((Get-Item $OutImg).Length / 1GB, 2)
Write-Host "=== DONE in $mins min ==="
Write-Host "image      : $OutImg ($sizeGbOut GB)"
Write-Host "build info : $OutImg.build-info.txt"
Write-Host 'Flash with Raspberry Pi Imager ("Use custom") or Win32DiskImager.'
