<#
.SYNOPSIS
  Bootstrap a Data Review station on Windows in one go (PowerShell 5.1 or later).

.DESCRIPTION
  1. fetch (or update) only the DataReview module folder from the git repository (sparse clone, ~1 MB),
  2. find the installed Slicer (optionally download and silently install it),
  3. run setup_review_station.py so Slicer starts directly in Data Review with the given manifest.

  One-liner on a fresh machine (the manifest must already exist, see README "Create the manifest"):

    powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/CavRiley/Slicer/data-review-module/Modules/Scripted/DataReview/Scripts/install_review_station.ps1))) -Manifest 'D:\dataset\review_manifest.json' -Reviewer 'JD'"

  or, when the dataset has no manifest yet (default layout adc\, tracew\, labels\): ... -Dataset 'D:\dataset' -Reviewer 'JD'

  Re-running the script updates the module (git pull) and re-applies the settings.
  Requires git (https://git-scm.com/download/win).

.PARAMETER Manifest
  review_manifest.json to open automatically at startup.
.PARAMETER Dataset
  Dataset folder without a manifest yet: scan it (adc\, tracew\, labels\ by default) and create
  review_manifest.json in it. -AdcPattern, -TracewPattern and -SegPattern override the layout.
  One of -Manifest / -Dataset is required unless -SkipSetup or -Reset.
.PARAMETER Reviewer
  Reviewer name stored with each decision.
.PARAMETER Dest
  Where to keep the module checkout (default: %USERPROFILE%\DataReview). An existing full clone of the
  repository is used as it is (no pull, no sparse checkout).
.PARAMETER Slicer
  Path to Slicer.exe (default: auto-detect).
.PARAMETER InstallSlicer
  Download the latest stable Slicer and install it silently if none is found.
.PARAMETER FullUI
  Keep the regular Slicer menus and toolbars.
.PARAMETER AllModules
  Load every Slicer module. By default only the modules Data Review needs are loaded (less memory, faster start).
.PARAMETER SkipSetup
  Only fetch/update the module, do not change Slicer settings.
.PARAMETER Reset
  Remove the Data Review startup settings from Slicer.
#>
[CmdletBinding()]
param(
  [string]$Manifest = "",
  [string]$Dataset = "",
  [string]$AdcPattern = "",
  [string]$TracewPattern = "",
  [string]$SegPattern = "",
  [string]$Reviewer = "",
  [string]$Dest = (Join-Path $env:USERPROFILE "DataReview"),
  [string]$Repo = "https://github.com/CavRiley/Slicer.git",
  [string]$Branch = "data-review-module",
  [string]$Slicer = "",
  [switch]$InstallSlicer,
  [switch]$FullUI,
  [switch]$AllModules,
  [switch]$SkipSetup,
  [switch]$Reset
)

$ErrorActionPreference = "Stop"
$ModuleSubdir = "Modules/Scripted/DataReview"

if (-not $Manifest -and -not $Dataset -and -not $SkipSetup -and -not $Reset) {
  Write-Error "-Manifest or -Dataset is required (or use -SkipSetup / -Reset)"
  exit 2
}
if ($Dataset -and -not (Test-Path -LiteralPath $Dataset -PathType Container)) {
  Write-Error "dataset folder not found: $Dataset"
  exit 2
}
if ($Manifest -and -not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
  Write-Error "manifest not found: $Manifest"
  exit 2
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Error "git is required: install it from https://git-scm.com/download/win and run this script again"
  exit 2
}

# --- 1. Fetch or update the module folder ---------------------------------------------------------
Write-Host "==> Module checkout: $Dest"
if (Test-Path -LiteralPath (Join-Path $Dest ".git")) {
  $sparse = & git -C $Dest config --get core.sparseCheckout 2>$null
  if ($sparse -eq "true") {
    & git -C $Dest sparse-checkout set $ModuleSubdir
    & git -C $Dest pull --ff-only -q
    if ($LASTEXITCODE -ne 0) { Write-Error "git pull failed"; exit 1 }
    Write-Host "    updated to $(& git -C $Dest rev-parse --short HEAD)"
  } else {
    # A full clone of the repository (e.g. a development checkout): use it as it is, never pull or
    # convert it to a sparse checkout.
    Write-Host "    using existing clone as is ($(& git -C $Dest rev-parse --short HEAD), branch $(& git -C $Dest rev-parse --abbrev-ref HEAD))"
  }
} else {
  $parent = Split-Path -Parent $Dest
  if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  & git clone -q --depth 1 --filter=blob:none --sparse -b $Branch $Repo $Dest
  if ($LASTEXITCODE -ne 0) { Write-Error "git clone failed"; exit 1 }
  & git -C $Dest sparse-checkout set $ModuleSubdir
  Write-Host "    cloned $(& git -C $Dest rev-parse --short HEAD) of branch $Branch"
}
$ModuleDir = Join-Path $Dest ($ModuleSubdir -replace "/", "\")
if (-not (Test-Path -LiteralPath (Join-Path $ModuleDir "DataReview.py"))) {
  Write-Error "module not found at $ModuleDir"
  exit 1
}

# --- 2. Find Slicer -------------------------------------------------------------------------------
function Find-Slicer {
  if ($Slicer) { return $Slicer }
  $candidates = @()
  foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, "C:\")) {
    if (-not $root) { continue }
    $candidates += Get-ChildItem -Path (Join-Path $root "slicer.org\Slicer*\Slicer.exe") -ErrorAction SilentlyContinue
    $candidates += Get-ChildItem -Path (Join-Path $root "Slicer*\Slicer.exe") -ErrorAction SilentlyContinue
  }
  $cmd = Get-Command Slicer.exe -ErrorAction SilentlyContinue
  if ($cmd) { $candidates += Get-Item $cmd.Source }
  # Prefer the newest version (sort by the folder name, e.g. "Slicer 5.12.4")
  $best = $candidates | Sort-Object { $_.Directory.Name } -Descending | Select-Object -First 1
  if ($best) { return $best.FullName }
  return $null
}

function Install-SlicerWindows {
  $installer = Join-Path $env:TEMP "Slicer-install.exe"
  Write-Host "==> Downloading the latest stable Slicer for Windows (about 270 MB)"
  Invoke-WebRequest -Uri "https://download.slicer.org/download?os=win&stability=release" -OutFile $installer -UseBasicParsing
  Write-Host "==> Installing Slicer silently (per-user install under $env:LOCALAPPDATA\slicer.org)"
  $p = Start-Process -FilePath $installer -ArgumentList "/S" -Wait -PassThru
  if ($p.ExitCode -ne 0) { Write-Error "Slicer installer exited with code $($p.ExitCode)"; exit 1 }
  Remove-Item $installer -ErrorAction SilentlyContinue
}

$SlicerExe = Find-Slicer
if (-not $SlicerExe -and $InstallSlicer) {
  Install-SlicerWindows
  $SlicerExe = Find-Slicer
}
if (-not $SlicerExe -or -not (Test-Path -LiteralPath $SlicerExe -PathType Leaf)) {
  Write-Error "Slicer not found. Install it from https://download.slicer.org (or pass -Slicer 'C:\path\to\Slicer.exe', or add -InstallSlicer), then run this script again."
  exit 1
}
Write-Host "==> Slicer: $SlicerExe"

# --- 3. Configure Slicer ---------------------------------------------------------------------------
if ($SkipSetup) {
  Write-Host "==> Skipping Slicer setup (-SkipSetup). Module folder: $ModuleDir"
  exit 0
}
$Setup = Join-Path $ModuleDir "Scripts\setup_review_station.py"
$setupArgs = @("--no-splash", "--no-main-window", "--python-script", "`"$Setup`"")
if ($Reset) {
  $setupArgs += "--reset"
} else {
  if ($Manifest) { $setupArgs += @("--manifest", "`"$Manifest`"") }
  if ($Dataset) { $setupArgs += @("--dataset", "`"$Dataset`"") }
  if ($AdcPattern) { $setupArgs += @("--adc-pattern", "`"$AdcPattern`"") }
  if ($TracewPattern) { $setupArgs += @("--tracew-pattern", "`"$TracewPattern`"") }
  if ($SegPattern) { $setupArgs += @("--seg-pattern", "`"$SegPattern`"") }
  if ($Reviewer) { $setupArgs += @("--reviewer", "`"$Reviewer`"") }
  if ($FullUI) { $setupArgs += "--full-ui" }
  if ($AllModules) { $setupArgs += "--all-modules" }
}
Write-Host "==> Configuring Slicer (its output goes to the Slicer log, not to this window)"
# Slicer.exe is a launcher that waits for the application and forwards its exit code.
$proc = Start-Process -FilePath $SlicerExe -ArgumentList $setupArgs -Wait -PassThru -NoNewWindow
$status = $proc.ExitCode
if ($status -eq 3) {
  Write-Host ""
  Write-Host "WARNING: Slicer could not save the module path to its settings file." -ForegroundColor Yellow
  Write-Host "         Add this folder by hand in Slicer: Edit > Application Settings > Modules > Additional module paths:"
  Write-Host "         $ModuleDir"
  Write-Host "         then restart Slicer. Everything else is configured."
  exit 3
} elseif ($status -ne 0) {
  Write-Error "Slicer setup did not complete (exit code $status). Check the newest log in $env:TEMP\Slicer-$env:USERNAME (or Help > Report a problem in Slicer)."
  exit $status
}

if ($Reset) {
  Write-Host "Data Review startup settings removed."
} else {
  Write-Host ""
  Write-Host "Done. Start Slicer normally to begin reviewing:"
  Write-Host "  $SlicerExe"
  Write-Host "Ctrl+Shift+U shows the full interface; re-run with -Reset to restore a regular Slicer."
}
