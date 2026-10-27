<#
  OmniBots installer for Windows 10/11.

  One line (PowerShell):
    irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1 | iex

  With options:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1))) -InstallDir D:\OmniBots -NoShortcut

  What it does (and nothing else):
    1. checks for Python 3.12+ and Git (offers to install them with winget, asking first)
    2. downloads OmniBots into $InstallDir, by default %USERPROFILE%\.omnibots next to your bots' data, like
       ~/.omni (or updates it with git pull if it's already there)
    3. makes its own Python environment ($InstallDir\.venv) and installs the dependencies there
    4. installs the browser the bots use (Chromium via Playwright) unless -NoBrowser
    5. adds a Start menu shortcut "OmniBots" (unless -NoShortcut)
  Run it again to update: if OmniBots is running it asks to close it first (-StopRunning closes it without
  asking), backs up the database, then updates the code, the dependencies and the browser in one go.
  Your settings, bots and projects live in %USERPROFILE%\.omnibots (git ignores them) and your output folder: an
  install or update never touches them.
#>
param(
    [string]$InstallDir = $(if ($env:OMNIBOTS_DIR) { $env:OMNIBOTS_DIR } else { Join-Path $HOME ".omnibots" }),
    [string]$Branch = "master",
    [switch]$NoShortcut,
    [switch]$NoBrowser,
    [switch]$Yes,           # answer "yes" to installing missing Python/Git with winget
    [switch]$StopRunning,   # an update while OmniBots runs: close it without asking (the Windows installer asks itself)
    [string]$Source = "https://github.com/tattooinmtl/Omnibots.git"   # or a local clone / a git bundle (the Windows installer)
)

$ErrorActionPreference = "Stop"
$Repo = $Source
$GitHub = "https://github.com/tattooinmtl/Omnibots.git"
$FromGitHub = ($Repo -eq $GitHub)

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

function Installed-Version($dir) {
    $init = Join-Path $dir "omnibots\__init__.py"
    if (-not (Test-Path $init)) { return $null }
    $m = Select-String -Path $init -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return $null
}

function Test-Running($vpy) {
    # the running app answers on its pipe; "not running" is exit code 4
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $vpy -m omnibots --send status 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $old }
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

# 2. an update: close the running app (asked first), back up the database
$before = Installed-Version $InstallDir
$oldPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if ($before -and (Test-Path $oldPy)) {
    Say "OmniBots $before is installed here: updating it"
    if (Test-Running $oldPy) {
        if (-not ($StopRunning -or (Ask "OmniBots is running. Close it now to update? (Bots' work carries on after the update.)"))) {
            Fail "Close OmniBots first (tray -> Quit), then run this again. Nothing was changed."
        }
        Say "Closing OmniBots"
        $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        & $oldPy -m omnibots --send stop 2>&1 | Out-Null
        $ErrorActionPreference = $old
        $waited = 0
        while ((Test-Running $oldPy) -and $waited -lt 90) { Start-Sleep -Seconds 2; $waited += 2 }
        if (Test-Running $oldPy) { Fail "OmniBots didn't close. Quit it from the tray, then run this again. Nothing was changed." }
        Start-Sleep -Seconds 2                          # let it release its files
    }
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $saved = & $oldPy -c "from omnibots.backup import backup_closed; from omnibots.paths import resolve_home; print(backup_closed(resolve_home(), 'before-update') or '')" 2>$null
    $ErrorActionPreference = $old
    if ($LASTEXITCODE -eq 0 -and $saved) { Say "Backed up your bots' database: $saved" }
    elseif ($LASTEXITCODE -ne 0) { Say "The backup before the update didn't work; your data isn't touched by an update, so it goes on." "Yellow" }
}

# 3. download or update the code
if ((Test-Path (Join-Path $InstallDir ".git")) -and -not $FromGitHub) {
    # the Windows installer: the code it carries (a git bundle) moves this install forward, fast-forward only
    git -C $InstallDir fetch --quiet $Repo $Branch
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't read the code this installer carries. Nothing was changed." }
    git -C $InstallDir merge-base --is-ancestor FETCH_HEAD HEAD
    if ($LASTEXITCODE -eq 0) {
        Say "The code here already has this version (or newer): checking everything else"
    } else {
        Say "Updating the code from the installer"
        git -C $InstallDir merge --quiet --ff-only FETCH_HEAD
        if ($LASTEXITCODE -ne 0) { Fail "Couldn't update: $InstallDir has changes of its own that this installer doesn't have. Nothing was changed." }
    }
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
    Say "Updating OmniBots in $InstallDir"
    git -C $InstallDir fetch --quiet origin $Branch
    git -C $InstallDir checkout --quiet $Branch
    git -C $InstallDir pull --quiet --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't update (local changes in $InstallDir?). Fix them or reinstall into an empty folder." }
} elseif ((Test-Path (Join-Path $InstallDir "settings.toml")) -or (Test-Path (Join-Path $InstallDir "db"))) {
    # the OmniBots data folder (bots, database, settings): the code goes in next to it, like ~/.omni. git ignores
    # every data name (.gitignore), and a checkout never overwrites a file that's already there: it stops instead.
    Say "Installing OmniBots into $InstallDir, next to your bots' data (it stays as it is)"
    git -C $InstallDir init --quiet
    git -C $InstallDir remote add origin $Repo
    git -C $InstallDir fetch --quiet origin $Branch
    if ($LASTEXITCODE -eq 0) { git -C $InstallDir checkout --quiet -b $Branch --track "origin/$Branch" }
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Recurse -Force (Join-Path $InstallDir ".git")      # only the .git made just now; the data is untouched
        Fail "Couldn't put the code in $InstallDir (a file there has the same name as one of OmniBots' files?). Nothing was changed."
    }
} else {
    if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force | Select-Object -First 1)) {
        Fail "$InstallDir exists and isn't an OmniBots install. Pick another folder with -InstallDir."
    }
    Say "Downloading OmniBots into $InstallDir"
    git clone --quiet --branch $Branch $Repo $InstallDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed." }
}
if (-not $FromGitHub -and $Repo -match '\.bundle$') {
    # installed from the Windows installer's bundle: later updates (About -> Update, or the install line) come from GitHub
    git -C $InstallDir remote set-url origin $GitHub
}

# 4. its own Python environment + dependencies
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

# 5. the browser the bots use
if (-not $NoBrowser) {
    Say "Installing the bots' browser (Chromium)"
    & $vpy -m playwright install chromium 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Say "The browser didn't install; the bots' browser tools will be off. Run: $vpy -m playwright install chromium" "Yellow" }
}

# 6. the doctor: creates OmniBots' folders and settings, upgrades the database, and when Omni isn't
#    installed, OmniBots' own provider config in ~/.omnibots\config (keys go in encrypted, via Settings -> Providers)
Say "Checking the setup (doctor)"
& $vpy -m omnibots.doctor
if ($LASTEXITCODE -ne 0) { Say "The doctor found something to fix (see above). Run it again any time: $vpy -m omnibots.doctor" "Yellow" }

# 7. a Start menu shortcut
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
if ($before -and $before -ne $version) { Write-Host "  OmniBots updated: $before -> $version." -ForegroundColor Green }
elseif ($before) { Write-Host "  OmniBots $version is up to date (checked and repaired)." -ForegroundColor Green }
else { Write-Host "  OmniBots $version is installed." -ForegroundColor Green }
Say ($(if ($NoShortcut) { "Start it with:  " } else { "Start it from the Start menu (OmniBots), or run:  " }) + "$vpy -m omnibots") "Gray"
Say "Update any time by running the same install line again." "Gray"
Write-Host ""
