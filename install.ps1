# Installs the dependencies and starts the alarm, which registers itself at logon on its first run.
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$pythonw = Join-Path $dir '.venv\Scripts\pythonw.exe'

if (-not (Test-Path $pythonw)) {
    py -3 -m venv (Join-Path $dir '.venv')
}
& (Join-Path $dir '.venv\Scripts\python.exe') -m pip install --quiet --disable-pip-version-check -r (Join-Path $dir 'requirements.txt')

Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f (Join-Path $dir 'calendar_alarm.py')) -WorkingDirectory $dir
Write-Host 'Calendar Alarm installed and started.'
