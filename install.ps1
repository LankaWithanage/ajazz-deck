# deckd installer - run from inside the streamdock folder on the new PC:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Options: -NoAutostart (skip the login shortcut), -Uninstall (remove shortcut, stop deckd)
param([switch]$NoAutostart, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'deckd (Stream Dock).lnk'

function Stop-Deckd {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like '*deckd.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped deckd (pid $($_.ProcessId))" }
}

if ($Uninstall) {
    Stop-Deckd
    if (Test-Path $lnk) { Remove-Item $lnk; Write-Host 'Removed login shortcut' }
    Write-Host 'deckd uninstalled (files and your config were left in place).'
    return
}

# 1. Python 3.10+
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw 'Python not found. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and run this again.' }
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]'3.10') { throw "Python $ver is too old; need 3.10 or newer." }
$pyw = Join-Path (Split-Path $py.Source) 'pythonw.exe'
Write-Host "Python $ver at $($py.Source)"

# 2. Vendor software must not be running (it fights over the deck)
$vendor = Get-Process | Where-Object { $_.Path -like '*Stream Dock*' -or $_.Path -like '*HotSpot*' }
if ($vendor) { Write-Warning 'Stream Dock AJAZZ is running. Close or uninstall it, or deckd and it will fight over the deck.' }

# 3. Libraries
Write-Host 'Installing Python libraries...'
& python -m pip install --quiet --disable-pip-version-check -r (Join-Path $here 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# 4. Deck plugged in?
$found = & python -c "import hid; print(any(d['usage_page']==0xFFA0 for d in hid.enumerate(0x0300,0x3002)))"
if ($found -ne 'True') { Write-Warning 'AJAZZ AKP03E not detected. Plug it in; deckd will pick it up when it appears.' }

# 5. Autostart at login
if (-not $NoAutostart) {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $here 'deckd.py') + '"'
    $s.WorkingDirectory = $here
    $s.WindowStyle = 7
    $s.Save()
    Write-Host "Login shortcut: $lnk"
}

# 6. (Re)start now
Stop-Deckd
Start-Process -WindowStyle Hidden -FilePath $pyw -ArgumentList ('"' + (Join-Path $here 'deckd.py') + '"') -WorkingDirectory $here
Start-Sleep 5
try {
    $st = Invoke-RestMethod http://127.0.0.1:8765/api/state -TimeoutSec 5
    Write-Host ("deckd running. Deck connected: {0}" -f $st.connected) -ForegroundColor Green
    Write-Host 'Editor: http://127.0.0.1:8765   Spotify Like key sign-in: http://127.0.0.1:8765/spotify/login'
} catch {
    Write-Warning "deckd did not answer yet - check $(Join-Path $here 'deckd.log')"
}
