# Calendar Alarm — an alarm before every appointment

*[Version française](README.md)*

A small Windows program that runs in the background and rings **10 minutes
before every appointment** of your calendars (Infomaniak, Google, Outlook,
iCloud…), whether or not the appointment carries a reminder and whoever created
it. A window comes to the front with the title of the appointment and two
buttons: **Stop** and **Snooze 5 min**.

Calendars are read again every minute: an appointment added at the last minute
rings as soon as it is found.

## Installation

1. Run `CalendarAlarm-Setup-<version>.exe` (no administrator rights needed).
   The file is not signed, so Windows shows a SmartScreen warning: “More
   info” → “Run anyway”.
2. When the installation ends, the **“Accounts and calendars”** window opens.
   Pick your provider in the drop-down list, click “Add…” and follow the
   instructions shown. Repeat for every calendar to watch.

The program registers itself to start with Windows. Everything else goes
through a **right-click** on the icon in the notification area: test the alarm,
manage accounts and calendars, change the language (French or English), turn
off starting with Windows, quit.

## Supported calendars

| Provider | What to enter |
| --- | --- |
| Infomaniak (kSuite, Kalendar) | Synchronisation username and application password, shown on <https://config.infomaniak.com> (“My Contacts & Calendars” → “Manual synchronisation”) |
| Google Calendar | The secret address of the calendar: Settings → your calendar → “Integrate calendar” → “Secret address in iCal format” |
| Outlook / Microsoft 365 | The ICS link of the published calendar: Outlook on the web → Settings → Calendar → Shared calendars → “Publish a calendar” |
| Apple iCloud | Apple Account address and app-specific password (account.apple.com) |
| Yahoo, Fastmail | Account address and app password |
| Nextcloud | Server address followed by `/remote.php/dav`, user name and app password |
| Other CalDAV server | CalDAV address, username and password |
| Other iCal address (.ics) | An `https://` address returning a calendar in iCal format |

For a CalDAV account (Infomaniak, iCloud, Yahoo, Fastmail, Nextcloud), the
program then asks which calendars of the account to watch: yours is proposed by
default, the shared calendars of colleagues and rooms are not. Google and
Outlook need one address per calendar.

A secret address gives read access to the whole calendar: it is stored like a
password, do not share it.

An appointment present in two calendars (an invitation received on both) rings
only once.

## Behaviour

- The alarm loops until you click. Closing the window with the cross means
  “Snooze”, not “Stop”.
- An appointment found less than 10 minutes ahead, or started less than
  5 minutes ago (waking from sleep), rings immediately.
- Ignored: all-day events, cancelled appointments and declined invitations.
- Green icon: every account is syncing. Red icon and a notification: an account
  has had no successful sync for 5 minutes, its alarms can no longer be
  trusted.
- Passwords and iCal addresses are stored in the Windows Credential Manager.
  If a server refuses one, the program asks for it again.

Uninstalling: “Installed apps” in Windows. It also removes the settings and the
saved passwords.

Limits: Windows only; the PC must be on, with the session open. For calendars
read through an iCal address (Google, Outlook), how quickly a new appointment
shows up depends on the provider.

## Settings

`%LOCALAPPDATA%\calendar-alarm\config.json` (restart the program after a
change):

| Key | Default | Role |
| --- | --- | --- |
| `lead_minutes` | 10 | Delay before the appointment |
| `snooze_minutes` | 5 | Length of a snooze |
| `late_grace_minutes` | 5 | How late an appointment may still ring |
| `poll_seconds` | 60 | How often calendars are read again |
| `sound` | `C:\Windows\Media\Alarm01.wav` | `.wav` file of the alarm |
| `my_emails` | `[]` | Addresses used to recognise the invitations you declined |
| `language` | language of Windows | `"fr"` or `"en"` |

The log is in the same folder (`calendar-alarm.log`).

## From the sources

Python 3.12 or later, installed on Windows.

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1    # venv, dependencies, launch
.venv\Scripts\python.exe -m unittest                    # tests
powershell -ExecutionPolicy Bypass -File build.ps1      # builds the installer in dist\ (requires Inno Setup 6)
powershell -ExecutionPolicy Bypass -File uninstall.ps1  # stops the program and removes it from startup
```

Every displayed text lives in `texts.py`, in French and in English.
