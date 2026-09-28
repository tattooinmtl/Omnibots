<#
  OmniBots installer for Windows 10/11.

  One line (PowerShell):
    irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1 | iex

  With options:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1))) -InstallDir D:\OmniBots -NoShortcut

  What it does (and nothing else):
    1. checks for Python 3.12+ and Git (offers to install them with winget, asking first)
    2. downloads OmniBots into $InstallDir (or updates it with git pull if it's already there)
    3. makes its own Python environment ($InstallDir\.venv) and installs the dependencies there
    4. installs the browser the bots use (Chromium via Playwright) unless -NoBrowser
    5. adds a Start menu shortcut "OmniBots" (unless -NoShortcut)
  Your settings, bots and projects live in %USERPROFILE%\.omnibots and your output folder: an update never touches them.
#>
param(
    [string]$InstallDir = $(if ($env:OMNIBOTS_DIR) { $env:OMNIBOTS_DIR } else { Join-Path $env:LOCALAPPDATA "OmniBots" }),
    [string]$Branch = "master",
    [switch]$NoShortcut,
    [switch]$NoBrowser,
    [switch]$Yes,           # answer "yes" to installing missing Python/Git with winget
    [string]$Source = "https://github.com/tattooinmtl/Omnibots.git"   # (for testing: a local clone)
)

$ErrorActionPreference = "Stop"
$Repo = $Source

function Say($text, $color = "Cyan") { Write-Host "  $text" -ForegroundColor $color }
function Fail($text) { Write-Host "`n  X $text`n" -ForegroundColor Red; throw $text }

function Ask($question) {
    if ($Yes) { return $true }
    try { $a = Read-Host "  $question [y/N]" } catch { return $false }
    return $a -match '^(y|yes|o|oui)$'
}

function Find-Python {
    foreach ($cmd in @("py -3.12", "py -3", "python", "python3")) {
        $exe, $arg = $cmd.Split(" ", 2)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @($arg | Where-Object { $_ }) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($v -and [version]$v -ge [version]"3.12") { return ,@($exe, $arg) }
    }
    return $null
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host ""
Write-Host "  OmniBots installer" -ForegroundColor White
Write-Host "  ------------------" -ForegroundColor DarkGray

# 1. Python 3.12+ and Git
$py = Find-Python
if (-not $py) {
    Say "Python 3.12 or newer is needed." "Yellow"
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Python 3.12 with winget now?")) {
        winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $py = Find-Python
    }
    if (-not $py) { Fail "Install Python 3.12+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this again." }
}
Say ("Python: " + (& $py[0] @($py[1] | Where-Object { $_ }) --version))
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say "Git is needed." "Yellow"
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Git with winget now?")) {
        winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Install Git from https://git-scm.com/download/win, then run this again." }
}
Say ("Git: " + (git --version))

# 2. download or update
if (Test-Path (Join-Path $InstallDir ".git")) {
    Say "Updating OmniBots in $InstallDir"
    git -C $InstallDir fetch --quiet origin $Branch
    git -C $InstallDir checkout --quiet $Branch
    git -C $InstallDir pull --quiet --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't update (local changes in $InstallDir?). Fix them or reinstall into an empty folder." }
} else {
    if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force | Select-Object -First 1)) {
        Fail "$InstallDir exists and isn't an OmniBots install. Pick another folder with -InstallDir."
    }
    Say "Downloading OmniBots into $InstallDir"
    git clone --quiet --branch $Branch $Repo $InstallDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed." }
}

# 3. its own Python environment + dependencies
$venv = Join-Path $InstallDir ".venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Say "Creating a Python environment"
    & $py[0] @($py[1] | Where-Object { $_ }) -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't create the Python environment." }
}
Say "Installing dependencies (this takes a minute the first time)"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $InstallDir "requirements.lock")
if ($LASTEXITCODE -ne 0) { Fail "Installing the dependencies failed (see the messages above)." }
# OmniBots itself (editable: it runs the cloned code, so a git pull updates it),
# so "python -m omnibots" works from any folder
& $vpy -m pip install --quiet --no-deps -e $InstallDir
if ($LASTEXITCODE -ne 0) { Fail "Installing OmniBots failed (see the messages above)." }

# 4. the browser the bots use
if (-not $NoBrowser) {
    Say "Installing the bots' browser (Chromium)"
    & $vpy -m playwright install chromium 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Say "The browser didn't install; the bots' browser tools will be off. Run: $vpy -m playwright install chromium" "Yellow" }
}

# 5. a Start menu shortcut
$version = (& $vpy -c "import omnibots; print(omnibots.__version__)" 2>$null)
if (-not $NoShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "OmniBots.lnk"
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sh.TargetPath = Join-Path $venv "Scripts\pythonw.exe"
    $sh.Arguments = "-m omnibots"
    $sh.WorkingDirectory = $InstallDir
    $sh.IconLocation = Join-Path $InstallDir "omnibots\ui\assets\omi.ico"
    $sh.Description = "OmniBots - one team of AI bots"
    $sh.Save()
    Say "Start menu shortcut: OmniBots"
}

Write-Host ""
Write-Host "  OmniBots $version is installed." -ForegroundColor Green
Say ($(if ($NoShortcut) { "Start it with:  " } else { "Start it from the Start menu (OmniBots), or run:  " }) + "$vpy -m omnibots") "Gray"
Say "Update any time by running the same install line again." "Gray"
Write-Host ""
