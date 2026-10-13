# Builds dist\CalendarAlarm.exe, a standalone executable that needs no Python on the target PC.
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$python = Join-Path $dir '.venv\Scripts\python.exe'
& $python -m pip install --quiet --disable-pip-version-check pyinstaller
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name CalendarAlarm `
    --collect-submodules caldav --collect-submodules icalendar --collect-submodules icalendar_searcher `
    --collect-submodules recurring_ical_events --collect-submodules x_wr_timezone --collect-submodules keyring `
    --collect-data icalendar --collect-data tzdata `
    --distpath (Join-Path $dir 'dist') --workpath (Join-Path $dir 'build') --specpath (Join-Path $dir 'build') `
    (Join-Path $dir 'calendar_alarm.py')
