# Stops the alarm and removes it from logon. The folder, the settings and the saved password are left in place.
$dir = $PSScriptRoot
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'CalendarAlarm' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -eq 'pythonw.exe' -and $_.CommandLine -like "*$dir*") -or $_.Name -eq 'CalendarAlarm.exe' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host 'Calendar Alarm stopped and removed from logon.'
Write-Host 'Saved password: Credential Manager > Windows Credentials > calendar-alarm.'
