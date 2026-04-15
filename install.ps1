# Pod The Trader — Windows one-shot installer.
#
#   irm https://raw.githubusercontent.com/Sortis-AI/pod_trades/main/install.ps1 | iex
#
# Installs git + uv + Python 3.12, clones the repo, syncs dependencies,
# and drops a launcher at %USERPROFILE%\.local\bin\pod-the-trader.cmd.
#
# Package manager priority: winget -> choco -> manual instructions.
#
# Environment overrides:
#   POD_TRADER_REPO   git URL to clone from (default: github.com/Sortis-AI/pod_trades)
#   POD_TRADER_DIR    install directory     (default: %USERPROFILE%\pod-the-trader)
#   POD_TRADER_REF    branch/tag/ref        (default: main)

#Requires -Version 5.1

$ErrorActionPreference = "Stop"

# ---- Config -----------------------------------------------------------------

$RepoUrl    = if ($env:POD_TRADER_REPO) { $env:POD_TRADER_REPO } else { "https://github.com/Sortis-AI/pod_trades.git" }
$InstallDir = if ($env:POD_TRADER_DIR)  { $env:POD_TRADER_DIR }  else { Join-Path $env:USERPROFILE "pod-the-trader" }
$Ref        = if ($env:POD_TRADER_REF)  { $env:POD_TRADER_REF }  else { "main" }
$LocalBin   = Join-Path $env:USERPROFILE ".local\bin"

# ---- Output helpers ---------------------------------------------------------

function Write-Bold($msg) { Write-Host $msg -ForegroundColor White }
function Write-Info($msg) { Write-Host "  > $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "  x $msg" -ForegroundColor Red
    exit 1
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ---- Package install helpers ------------------------------------------------

function Install-Package($pkg, $wingetId) {
    # winget is preferred — ships with every supported Windows 11 and
    # recent Windows 10 builds. Fall back to choco for people who have
    # it pre-installed, otherwise bail with clear guidance.
    if (Test-Command "winget") {
        Write-Info "winget install $wingetId"
        try {
            winget install --id $wingetId --accept-source-agreements --accept-package-agreements --silent --disable-interactivity | Out-Null
            Write-Ok "installed $pkg via winget"
            return
        } catch {
            Write-Warn2 "winget couldn't install $pkg ($_); trying choco"
        }
    }
    if (Test-Command "choco") {
        Write-Info "choco install $pkg"
        choco install $pkg -y --no-progress | Out-Null
        Write-Ok "installed $pkg via choco"
        return
    }
    Fail "No supported package manager (need winget or choco) to install $pkg. Install winget from the Microsoft Store or follow the manual install instructions for $pkg."
}

# ---- Prerequisite installers ------------------------------------------------

function Install-Git {
    if (Test-Command "git") {
        $v = (git --version) -join ""
        Write-Ok "git already installed ($v)"
        return
    }
    Write-Info "git not found - installing"
    Install-Package "git" "Git.Git"
    # winget installs don't propagate PATH to the current process.
    # Re-probe via the default install path.
    $defaultGit = "$env:ProgramFiles\Git\cmd"
    if (Test-Path $defaultGit) {
        $env:Path = "$defaultGit;$env:Path"
    }
    if (-not (Test-Command "git")) {
        Fail "git installed but not on PATH. Open a new shell and re-run this script."
    }
}

function Install-Uv {
    if (Test-Command "uv") {
        $v = (uv --version) -join ""
        Write-Ok "uv already installed ($v)"
        return
    }
    Write-Info "uv not found - installing via astral.sh PowerShell installer"
    # Official installer drops uv into ~\.local\bin and updates user PATH.
    # We re-export for the rest of this process.
    try {
        Invoke-RestMethod -UseBasicParsing https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Fail "uv install failed: $_"
    }
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvBin) {
        $env:Path = "$uvBin;$env:Path"
    }
    if (-not (Test-Command "uv")) {
        Fail "uv install succeeded but the binary is not on PATH. Open a new shell and re-run."
    }
    $v = (uv --version) -join ""
    Write-Ok "installed uv ($v)"
}

function Install-Python {
    # uv can manage Python itself, but we still sanity-check that something
    # usable is available so `uv sync` won't explode later.
    if (Test-Command "python") {
        $versionLine = (python --version 2>&1) -join ""
        if ($versionLine -match "Python (\d+)\.(\d+)") {
            $major = [int]$matches[1]; $minor = [int]$matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
                Write-Ok "system python is $versionLine"
                return
            }
        }
    }
    Write-Info "python 3.12+ not found - letting uv manage it"
    uv python install 3.12 | Out-Null
    Write-Ok "installed python 3.12 via uv"
}

# ---- Repo clone + sync ------------------------------------------------------

function Clone-Repo {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        Write-Info "updating existing checkout at $InstallDir"
        git -C $InstallDir fetch --quiet origin $Ref
        git -C $InstallDir checkout --quiet $Ref
        try {
            git -C $InstallDir reset --hard --quiet "origin/$Ref"
        } catch {
            git -C $InstallDir reset --hard --quiet $Ref
        }
        Write-Ok "updated $InstallDir to $Ref"
    } else {
        Write-Info "cloning $RepoUrl -> $InstallDir"
        git clone --quiet --branch $Ref $RepoUrl $InstallDir
        Write-Ok "cloned into $InstallDir"
    }
}

function Sync-Deps {
    Write-Info "installing dependencies via uv sync"
    Push-Location $InstallDir
    try {
        uv sync --quiet
    } finally {
        Pop-Location
    }
    Write-Ok "dependencies installed"
}

# ---- Launcher ---------------------------------------------------------------

function Install-Launcher {
    if (-not (Test-Path $LocalBin)) {
        New-Item -ItemType Directory -Path $LocalBin -Force | Out-Null
    }
    $launcher = Join-Path $LocalBin "pod-the-trader.cmd"
    # Tiny shim: cd into the install dir and exec uv. The `update`
    # subcommand lives inside the Python entry point now, so this
    # launcher has nothing else to do.
    $cmdContent = @"
@echo off
REM Generated by pod-the-trader install.ps1 - do not edit by hand.
REM Re-run the installer to refresh this launcher.
cd /d "$InstallDir"
uv run pod-the-trader %*
"@
    Set-Content -Path $launcher -Value $cmdContent -Encoding ASCII
    Write-Ok "launcher installed at $launcher"
}

# ---- PATH / profile ---------------------------------------------------------

function Ensure-LocalBinOnPath {
    # If $LocalBin is already on the persisted user PATH, nothing to do.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) { $parts = $userPath.Split(";") }
    if ($parts -contains $LocalBin) {
        Write-Ok "$LocalBin already on user PATH"
        $script:PathUpdated = $false
        return
    }

    $newUserPath = if ($userPath) { "$userPath;$LocalBin" } else { $LocalBin }
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    Write-Ok "added $LocalBin to user PATH"
    # Also update the current process so subsequent checks see it.
    $env:Path = "$env:Path;$LocalBin"
    $script:PathUpdated = $true
}

# ---- Main -------------------------------------------------------------------

Write-Bold "Pod The Trader - installer (Windows)"
Write-Host "  OS:           windows"
Write-Host "  install dir:  $InstallDir"
Write-Host "  repo:         $RepoUrl"
Write-Host "  ref:          $Ref"
Write-Host ""

$script:PathUpdated = $false

Write-Bold "[1/6] Prerequisites"
Install-Git
Install-Uv
Install-Python
Write-Host ""

Write-Bold "[2/6] Source"
Clone-Repo
Write-Host ""

Write-Bold "[3/6] Dependencies"
Sync-Deps
Write-Host ""

Write-Bold "[4/6] Launcher"
Install-Launcher
Write-Host ""

Write-Bold "[5/6] Shell PATH"
Ensure-LocalBinOnPath
Write-Host ""

Write-Bold "[6/6] Done"
Write-Host ""
if ($script:PathUpdated) {
    Write-Host "  Open a new PowerShell or Windows Terminal window so the"
    Write-Host "  updated PATH takes effect, then start the bot with:"
} else {
    Write-Host "  Start the bot with:"
}
Write-Host ""
Write-Host "      pod-the-trader"
Write-Host ""
Write-Host "  On first launch you will be asked to accept a disclaimer."
Write-Host '  You must type "I ACCEPT" to continue.'
Write-Host ""
Write-Host "  For best results, run pod-the-trader in Windows Terminal"
Write-Host "  (not legacy cmd.exe) so the TUI renders correctly."
Write-Host ""
Write-Host "  To upgrade to the latest version later, run:"
Write-Host ""
Write-Host "      pod-the-trader update"
Write-Host ""
