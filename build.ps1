# Builds dist\CalendarAlarm.exe, a standalone executable that needs no Python on the target PC.
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$python = Join-Path $dir '.venv\Scripts\python.exe'
& $python -m pip install --quiet --disable-pip-version-check pyinstaller
$icon = Join-Path $dir 'build\CalendarAlarm.ico'
New-Item -ItemType Directory -Force (Join-Path $dir 'build') | Out-Null
Set-Location $dir
& $python -c "import calendar_alarm as c; c.make_icon(c.COLOR_OK).save(r'$icon', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 256)])"
& $python -m PyInstaller --noconfirm --onefile --windowed --name CalendarAlarm --icon $icon `
    --collect-submodules caldav --collect-submodules icalendar --collect-submodules icalendar_searcher `
    --collect-submodules recurring_ical_events --collect-submodules x_wr_timezone --collect-submodules keyring `
    --collect-data icalendar --collect-data tzdata `
    --distpath (Join-Path $dir 'dist') --workpath (Join-Path $dir 'build') --specpath (Join-Path $dir 'build') `
    (Join-Path $dir 'calendar_alarm.py')
