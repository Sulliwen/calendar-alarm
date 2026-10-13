# Builds dist\CalendarAlarm-Setup-<version>.exe, an installer that needs no Python on the target PC.
# Requires Inno Setup 6 (winget install JRSoftware.InnoSetup).
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$python = Join-Path $dir '.venv\Scripts\python.exe'
& $python -m pip install --quiet --disable-pip-version-check pyinstaller
$icon = Join-Path $dir 'build\CalendarAlarm.ico'
New-Item -ItemType Directory -Force (Join-Path $dir 'build') | Out-Null
Set-Location $dir
& $python -c "import calendar_alarm as c; c.make_icon(c.COLOR_OK).save(r'$icon', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 256)])"
& $python -m PyInstaller --noconfirm --onedir --windowed --name CalendarAlarm --icon $icon `
    --collect-submodules caldav --collect-submodules icalendar --collect-submodules icalendar_searcher `
    --collect-submodules recurring_ical_events --collect-submodules x_wr_timezone --collect-submodules keyring `
    --collect-data icalendar --collect-data tzdata `
    --distpath (Join-Path $dir 'dist') --workpath (Join-Path $dir 'build') --specpath (Join-Path $dir 'build') `
    (Join-Path $dir 'calendar_alarm.py')

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 not found.' }
& $iscc /Q (Join-Path $dir 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }

Get-ChildItem (Join-Path $dir 'dist\CalendarAlarm-Setup-*.exe') | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    Set-Content -Path "$($_.FullName).sha256" -Value "$hash  $($_.Name)" -Encoding ascii
    Write-Host "$($_.Name)  SHA-256: $hash"
}
