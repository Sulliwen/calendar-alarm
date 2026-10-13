"""Calendar alarm: rings before every event of your calendars.

Runs in the background with a tray icon. Every event gets an alarm, whether
or not it carries a reminder and whoever created it. Calendars are read from
CalDAV accounts (Infomaniak, Nextcloud, iCloud...) and from iCal addresses
(Google Calendar, Outlook...).
"""

import copy
import ctypes
import gzip
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import types
import urllib.error
import urllib.request
import uuid
import winreg
import winsound
from dataclasses import dataclass
from datetime import datetime, time as clock, timedelta
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import unquote, urlsplit

import caldav
import icalendar
import keyring
import keyring.errors
import pystray
import recurring_ical_events
from caldav.lib.error import AuthorizationError
from PIL import Image, ImageDraw, ImageTk

import texts
from texts import PROVIDERS, t

# MAJOR.MINOR.PATCH (semver.org): PATCH for a fix, MINOR for a new feature, MAJOR when an update
# cannot keep the user's settings. The installer and the settings window read it from here.
__version__ = "1.0.0"

APP = "calendar-alarm"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "calendar-alarm.log"

INFOMANIAK_URL = "https://sync.infomaniak.com"
DEFAULT_CONFIG = {
    # CalDAV: {"id", "type": "caldav", "provider", "url", "username", "calendars": [urls] or None until chosen}
    # iCal:   {"id", "type": "ics", "provider", "name"}
    # Passwords and iCal addresses live in the Windows Credential Manager, not here.
    "accounts": [],
    # "fr" or "en"; None follows the language of Windows.
    "language": None,
    "lead_minutes": 10,
    "snooze_minutes": 5,
    # The sound stops by itself after this many minutes, the window stays; 0 means never.
    "silence_after_minutes": 0,
    # Appointments marked "free" (shown as available) are displayed but do not ring.
    "ignore_free": False,
    # Days read ahead, today included: the alarms need the next minutes, the agenda window the rest.
    "agenda_days": 7,
    "show_all_day": True,
    # An event found after it started still rings during this many minutes.
    "late_grace_minutes": 5,
    "poll_seconds": 60,
    "sound": r"C:\Windows\Media\Alarm01.wav",
    # Extra addresses used to recognise the invitations you declined.
    "my_emails": [],
}

SILENCE_CHOICES = [0, 1, 2, 5, 10]
POLL_CHOICES = [60, 120, 300]
RETRY_SECONDS = 15
TICK_MS = 500
REDISCOVER_SECONDS = 1800
TEST_PREFIX = "test:"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CalendarAlarm"
COLOR_OK = "#2e7d32"
COLOR_ERROR = "#c62828"
FONT = "Segoe UI"
COLOR_TEXT = "#202124"
COLOR_MUTED = "#80868b"
COLOR_ACCENT = "#1a73e8"
COLOR_HIGHLIGHT = "#e8f0fe"
COLOR_LINE = "#e3e5e8"
COLOR_PANEL = "#f6f7f8"
# One colour per watched calendar, in the order they were added.
CALENDAR_COLORS = ["#1a73e8", "#e67c00", "#0b8043", "#8e24aa", "#d93025", "#00897b", "#c2185b", "#5f6368"]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

log = logging.getLogger(APP)


@dataclass(frozen=True)
class Event:
    key: str
    start: datetime
    end: datetime
    summary: str
    location: str
    calendar: str
    # All-day events are shown in the agenda but never ring.
    all_day: bool = False
    # Marked as "free" by its owner: it does not block the time slot.
    free: bool = False


class Refused(Exception):
    """The server rejects the saved password or iCal address."""


class CalendarsNeeded(Exception):
    """The calendars to watch are not chosen yet, or one of them no longer exists."""


@dataclass
class Discovery:
    # url -> (caldav calendar, display name), for every calendar holding events
    calendars: dict
    addresses: set
    owner_name: str


# --- storage ----------------------------------------------------------------


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def secret_service(account):
    return f"{APP}:{account['id']}"


def get_secret(account):
    return keyring.get_password(secret_service(account), "secret")


def set_secret(account, secret):
    keyring.set_password(secret_service(account), "secret", secret)


def delete_secret(account):
    try:
        keyring.delete_password(secret_service(account), "secret")
    except keyring.errors.PasswordDeleteError:
        pass


def load_config():
    stored = load_json(CONFIG_PATH, {})
    if "username" in stored:
        # Builds made before the first release knew a single Infomaniak account, kept at the top level.
        username = stored.pop("username")
        account = {
            "id": uuid.uuid4().hex[:8],
            "type": "caldav",
            "url": stored.pop("url", INFOMANIAK_URL),
            "username": username,
            "calendars": stored.pop("calendars", None),
        }
        password = keyring.get_password(APP, username)
        if password:
            set_secret(account, password)
            keyring.delete_password(APP, username)
        stored["accounts"] = [account]
        save_json(CONFIG_PATH, stored)
        log.info("configuration migrated to the multi-account format")
    return {**copy.deepcopy(DEFAULT_CONFIG), **stored}


def provider_of(account):
    """The provider an account was created from; older accounts did not record it."""
    key = account.get("provider")
    if key is None:
        infomaniak = account["type"] == "caldav" and account["url"].startswith(INFOMANIAK_URL)
        key = "infomaniak" if infomaniak else account["type"]
    return next(provider for provider in PROVIDERS if provider["key"] == key)


def account_label(account):
    if account["type"] == "ics":
        return account["name"]
    return f"{account['username']} — {urlsplit(account['url']).hostname}"


# --- autostart --------------------------------------------------------------


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def autostart_command():
    """The command registered at logon for the current user, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        return None


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


# --- reading calendars ------------------------------------------------------


def is_declined(component, my_addresses):
    attendees = component.get("ATTENDEE") or []
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        address = str(attendee).lower().removeprefix("mailto:")
        if address in my_addresses and str(attendee.params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def extract_events(components, calendar_name, my_addresses):
    """Non-cancelled, non-declined events among (already expanded) VEVENT components."""

    def local(value):
        # An all-day event carries a date: it runs from midnight to midnight, local time.
        # astimezone() reads a naive (floating) datetime as local time.
        return (value if isinstance(value, datetime) else datetime.combine(value, clock.min)).astimezone()

    events = []
    for component in components:
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if is_declined(component, my_addresses):
            continue
        all_day = not isinstance(dtstart.dt, datetime)
        start = local(dtstart.dt)
        if component.get("DTEND") is not None:
            end = local(component.get("DTEND").dt)
        elif component.get("DURATION") is not None:
            end = start + component.get("DURATION").dt
        else:
            end = start + timedelta(days=1) if all_day else start
        events.append(
            Event(
                # No account in the key: a meeting present in two calendars rings once.
                key=f"{component.get('UID')}|{start.isoformat()}",
                start=start,
                end=end,
                summary=str(component.get("SUMMARY") or t("untitled")),
                location=str(component.get("LOCATION") or ""),
                calendar=calendar_name,
                all_day=all_day,
                free=str(component.get("TRANSP", "")).upper() == "TRANSPARENT",
            )
        )
    return events


def due_events(events, handled, now, lead, grace):
    """Timed events whose alarm window is open and that were neither stopped, snoozed nor ringing."""
    return [
        event
        for event in events
        if not event.all_day and event.key not in handled and event.start - lead <= now < event.start + grace
    ]


def agenda_days(events, today, days):
    """[(date, its events)] for the given number of days: all-day events first, then by start time."""
    agenda = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        on_that_day = [
            event
            for event in events
            # An all-day event ends at the midnight that follows its last day.
            if event.start.date() <= day
            and day <= ((event.end - timedelta(seconds=1)).date() if event.all_day else event.start.date())
        ]
        on_that_day.sort(key=lambda event: (not event.all_day, event.start, event.summary))
        agenda.append((day, on_that_day))
    return agenda


def discover(url, username, password, config):
    """Lists the calendars of a CalDAV account."""
    try:
        client = caldav.DAVClient(url=url, username=username, password=password, timeout=30)
        principal = client.principal()
        found = principal.calendars()
    except AuthorizationError as exc:
        raise Refused from exc
    calendars = {}
    for calendar in found:
        try:
            name = calendar.get_display_name() or str(calendar.url)
        except Exception:
            name = str(calendar.url)
        try:
            if "VEVENT" not in calendar.get_supported_components():
                continue
        except Exception:
            pass
        calendars[str(calendar.url)] = (calendar, name)

    addresses = {email.lower() for email in config["my_emails"]}
    if "@" in username:
        addresses.add(username.lower())
    try:
        for address in principal.calendar_user_address_set():
            addresses.add(str(address).lower().removeprefix("mailto:"))
    except Exception as exc:
        log.info("no calendar-user-address-set: %r", exc)
    try:
        owner_name = principal.get_display_name() or ""
    except Exception:
        owner_name = ""
    return Discovery(calendars, addresses, owner_name)


def alarm_sounds():
    """{name: path} of the alarm sounds that come with Windows."""
    media = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media"
    return {
        f"{path.stem[:-2]} {path.stem[-2:]}": str(path)
        for pattern in ("Alarm??.wav", "Ring??.wav")
        for path in sorted(media.glob(pattern))
    }


def normalize_ics_url(url):
    """Returns the https address of an iCal feed, or None when it is not one."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    return url if url.lower().startswith("https://") else None


def download_ics(url):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": APP})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as exc:
        # A secret address that was reset answers as if it never existed.
        if exc.code in (401, 403, 404, 410):
            raise Refused from exc
        raise
    return body


class Fetcher(threading.Thread):
    """Polls every account and hands its upcoming events to the UI thread."""

    def __init__(self, app):
        super().__init__(daemon=True, name="fetcher")
        self.app = app
        self.wake = threading.Event()
        # Accounts waiting for the user (refused secret, calendars to choose).
        self.blocked = set()
        # account id -> (Discovery, monotonic time of the discovery)
        self.discoveries = {}
        # account id -> (digest of the feed, expandable calendar)
        self.feeds = {}

    def reset(self):
        self.blocked.clear()
        self.discoveries.clear()
        self.wake.set()

    def run(self):
        while True:
            delay = self.cycle()
            self.wake.wait(delay)
            self.wake.clear()

    def cycle(self):
        app = self.app
        failed = False
        for account in list(app.config["accounts"]):
            account_id = account["id"]
            if account_id in self.blocked:
                continue
            try:
                secret = get_secret(account)
                if not secret:
                    raise Refused
                events = self.fetch(account, secret)
            except Refused:
                log.warning("%s: secret refused by the server", account_label(account))
                self.blocked.add(account_id)
                self.discoveries.pop(account_id, None)
                app.post(partial(app.on_refused, account_id))
            except CalendarsNeeded:
                self.blocked.add(account_id)
                app.post(partial(app.ask_calendars, account_id))
            except Exception as exc:
                log.warning("%s: sync failed: %r", account_label(account), exc)
                self.discoveries.pop(account_id, None)
                failed = True
                app.post(partial(app.on_sync_error, account_id, str(exc) or type(exc).__name__))
            else:
                app.post(partial(app.on_events, account_id, events))
        return RETRY_SECONDS if failed else app.config["poll_seconds"]

    def fetch(self, account, secret):
        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = min(midnight, now - timedelta(minutes=self.app.config["late_grace_minutes"]))
        end = midnight + timedelta(days=self.app.config["agenda_days"])
        if account["type"] == "ics":
            return self.fetch_ics(account, secret, start, end)
        return self.fetch_caldav(account, secret, start, end)

    def fetch_caldav(self, account, password, start, end):
        account_id = account["id"]
        discovery, discovered_at = self.discoveries.get(account_id, (None, 0.0))
        if discovery is None or time.monotonic() - discovered_at > REDISCOVER_SECONDS:
            discovery = discover(account["url"], account["username"], password, self.app.config)
            self.discoveries[account_id] = (discovery, time.monotonic())
            names = {url: name for url, (_, name) in discovery.calendars.items()}
            self.app.post(partial(self.app.on_discovery, account_id, names, discovery.owner_name))
        selected = account["calendars"]
        if selected is None or not set(selected) <= discovery.calendars.keys():
            raise CalendarsNeeded
        events = []
        for calendar, name in (discovery.calendars[url] for url in selected):
            for found in calendar.search(start=start, end=end, event=True, expand=True):
                components = found.icalendar_instance.walk("VEVENT")
                events.extend(extract_events(components, name, discovery.addresses))
        return events

    def fetch_ics(self, account, url, start, end):
        body = download_ics(url)
        digest = hashlib.sha256(body).digest()
        if self.feeds.get(account["id"], (None, None))[0] != digest:
            # Parsing a large feed is the costly part: only redo it when the feed changed.
            self.feeds[account["id"]] = (digest, recurring_ical_events.of(icalendar.Calendar.from_ical(body)))
        # The address of a Google feed holds the address of its owner.
        addresses = {email.lower() for email in EMAIL.findall(unquote(url))}
        addresses.update(email.lower() for email in self.app.config["my_emails"])
        components = self.feeds[account["id"]][1].between(start, end)
        return extract_events(components, account["name"], addresses)


# --- windows ----------------------------------------------------------------


def button(parent, text, command, kind="secondary", **options):
    """kind: "primary" (what the window is for), "secondary", or "link" (text only, inside a row)."""
    background = parent["bg"]
    styles = {
        "primary": dict(
            bg=COLOR_ACCENT, fg="white", activebackground="#174ea6", activeforeground="white",
            font=(FONT, 10, "bold"), padx=16, pady=5,
        ),
        "secondary": dict(bg="#e8eaed", fg=COLOR_TEXT, activebackground="#dadce0", font=(FONT, 10), padx=16, pady=5),
        "link": dict(
            bg=background, fg=COLOR_ACCENT, activebackground=background, activeforeground="#174ea6",
            font=(FONT, 9), padx=4, pady=0,
        ),
    }
    return tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2", **{**styles[kind], **options})


class Dialog(tk.Toplevel):
    """A settings window, open at most once per name: a header, a body (self.frame) and a button bar."""

    def __init__(self, app, name, title, subtitle=""):
        super().__init__(app.root)
        self.app = app
        self.name = name
        self.title(f"Calendar Alarm — {title}")
        self.configure(bg="white")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _: self.close())
        # Sized in characters, so that the window keeps its proportions on a scaled display.
        self.wrap = 60 * tkfont.Font(family=FONT, size=10).measure("0")

        header = tk.Frame(self, bg="white", padx=22, pady=14)
        header.pack(fill="x")
        tk.Label(header, text=title, font=(FONT, 15, "bold"), bg="white", fg=COLOR_TEXT).pack(anchor="w")
        if subtitle:
            tk.Label(
                header, text=subtitle, font=(FONT, 10), bg="white", fg=COLOR_MUTED, wraplength=self.wrap, justify="left"
            ).pack(anchor="w", pady=(4, 0))
        tk.Frame(self, bg=COLOR_LINE, height=1).pack(fill="x")
        self.footer = tk.Frame(self, bg=COLOR_PANEL, padx=22, pady=10)
        self.footer.pack(side="bottom", fill="x")
        self.frame = tk.Frame(self, bg="white", padx=22, pady=14)
        self.frame.pack(fill="both", expand=True)

    def label(self, parent, text, **options):
        options = {"font": (FONT, 10), "bg": parent["bg"], "fg": COLOR_TEXT, **options}
        return tk.Label(parent, text=text, **options)

    def field(self, grid, row, label, value, show=""):
        self.label(grid, label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 14))
        entry = tk.Entry(
            grid, font=(FONT, 10), width=40, show=show, relief="flat",
            highlightthickness=1, highlightbackground="#c4c7cc", highlightcolor=COLOR_ACCENT,
        )
        entry.insert(0, value)
        entry.grid(row=row, column=1, pady=5, ipady=4)
        return entry

    def buttons(self, submit_text, submit):
        self.status = self.label(self.frame, "", font=(FONT, 9), fg=COLOR_ERROR, wraplength=self.wrap, justify="left")
        self.status.pack(anchor="w", pady=(10, 0))
        button(self.footer, t("cancel"), self.close).pack(side="right")
        self.submit_button = button(self.footer, submit_text, submit, "primary")
        self.submit_button.pack(side="right", padx=(0, 8))

    def test(self, check, *args):
        """Runs check(*args) off the UI thread, then save(*args) unless it returned an error message."""
        self.submit_button.config(state="disabled")
        self.status.config(text=t("testing"), fg=COLOR_MUTED)

        def run():
            try:
                error = check(*args)
            except Refused:
                error = t("refused_input")
            except Exception as exc:
                error = t("connection_failed", error=str(exc) or type(exc).__name__)
            self.app.post(partial(self.tested, error, *args))

        threading.Thread(target=run, daemon=True).start()

    def tested(self, error, *args):
        if not self.winfo_exists():
            return
        if error:
            self.status.config(text=error, fg=COLOR_ERROR)
            self.submit_button.config(state="normal")
            return
        self.save(*args)
        self.close()

    def close(self):
        self.app.dialogs.pop(self.name, None)
        self.destroy()


class CaldavDialog(Dialog):
    def __init__(self, app, account, provider, message):
        super().__init__(app, "caldav", t(f"provider_{provider['key']}"), message)
        self.account = account
        self.provider = provider
        grid = tk.Frame(self.frame, bg="white")
        grid.pack(anchor="w")
        self.url = self.field(grid, 0, t("server"), account["url"] if account else provider["url"])
        self.username = self.field(grid, 1, t("username"), account["username"] if account else "")
        self.password = self.field(grid, 2, t("app_password"), "", show="•")
        self.buttons(t("test_and_save"), self.submit)
        self.bind("<Return>", lambda _: self.submit())
        (self.password if account else self.username).focus_set()

    def submit(self):
        url, username, password = self.url.get().strip(), self.username.get().strip(), self.password.get()
        if not (url and username and password):
            self.status.config(text=t("three_required"))
            return
        self.test(self.check, url, username, password)

    def check(self, url, username, password):
        if not discover(url, username, password, self.app.config).calendars:
            return t("no_calendar_found")

    def save(self, url, username, password):
        self.app.save_caldav_account(self.account, self.provider, url, username, password)


class IcsDialog(Dialog):
    def __init__(self, app, account, provider, message):
        super().__init__(app, "ics", t(f"provider_{provider['key']}"), message)
        self.account = account
        self.provider = provider
        grid = tk.Frame(self.frame, bg="white")
        grid.pack(anchor="w")
        # A named provider makes a sensible default name; "other" does not.
        default_name = "" if provider["key"] == "ics" else t(f"provider_{provider['key']}")
        self.label_entry = self.field(grid, 0, t("calendar_name"), account["name"] if account else default_name)
        self.url = self.field(grid, 1, t("ics_address"), "")
        self.buttons(t("test_and_save"), self.submit)
        self.bind("<Return>", lambda _: self.submit())
        self.url.focus_set()

    def submit(self):
        name, url = self.label_entry.get().strip(), normalize_ics_url(self.url.get())
        if not name or url is None:
            self.status.config(text=t("ics_invalid"))
            return
        self.test(self.check, name, url)

    def check(self, name, url):
        try:
            icalendar.Calendar.from_ical(download_ics(url))
        except ValueError:
            return t("not_ical")

    def save(self, name, url):
        self.app.save_ics_account(self.account, self.provider, name, url)


class CalendarsDialog(Dialog):
    def __init__(self, app, account):
        super().__init__(app, "calendars", t("title_calendars"), t("choose_calendars", name=account_label(account)))
        self.account = account
        names = app.calendar_names[account["id"]]
        if account["calendars"] is None:
            # First choice: propose the calendar named after the account owner.
            selected = {url for url, name in names.items() if name == app.owner_names.get(account["id"])}
        else:
            selected = set(account["calendars"])
        self.choices = {}
        for url, name in sorted(names.items(), key=lambda item: item[1].lower()):
            self.choices[url] = tk.BooleanVar(self, value=url in selected)
            tk.Checkbutton(
                self.frame, text=name, variable=self.choices[url], font=(FONT, 10),
                bg="white", activebackground="white", fg=COLOR_TEXT,
            ).pack(anchor="w")
        self.buttons(t("save"), self.submit)

    def submit(self):
        urls = [url for url, checked in self.choices.items() if checked.get()]
        if not urls:
            self.status.config(text=t("at_least_one"))
            return
        self.app.save_calendars(self.account, urls)
        self.close()


class AccountsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "accounts", t("title_accounts"), t("accounts_intro"))
        self.rows = tk.Frame(self.frame, bg="white")
        self.rows.pack(anchor="w", fill="x")
        add = tk.Frame(self.frame, bg="white")
        add.pack(anchor="w", pady=(16, 0))
        self.label(add, t("add_calendar")).pack(side="left", padx=(0, 8))
        names = [t(f"provider_{provider['key']}") for provider in PROVIDERS]
        self.provider = ttk.Combobox(add, state="readonly", values=names, width=30, font=(FONT, 10))
        self.provider.current(0)
        self.provider.pack(side="left", padx=(0, 8))
        # A label acting as a button: a real one cannot be made as low as the drop-down list beside it.
        add_button = tk.Label(
            add, text=t("add"), font=(FONT, 10, "bold"), bg=COLOR_ACCENT, fg="white", padx=14, pady=0,
            cursor="hand2", takefocus=1,
        )
        add_button.pack(side="left", fill="y")
        for trigger in ("<Button-1>", "<Return>", "<space>"):
            add_button.bind(trigger, lambda _: self.add())
        button(self.footer, t("close"), self.close).pack(side="right")
        self.refresh()

    def add(self):
        self.app.edit_account(None, PROVIDERS[self.provider.current()])

    def refresh(self):
        for child in self.rows.winfo_children():
            child.destroy()
        accounts = self.app.config["accounts"]
        if not accounts:
            self.label(self.rows, t("no_account"), fg=COLOR_MUTED).pack(anchor="w")
        for account in accounts:
            failing = account["id"] in self.app.sync_errors
            waiting = self.app.account_problem(account) is not None
            row = tk.Frame(self.rows, bg="white")
            row.pack(anchor="w", fill="x", pady=3)
            # Same coloured bar as an appointment in the agenda: here it tells the state of the account.
            bar = COLOR_ERROR if failing else COLOR_LINE if waiting else COLOR_OK
            tk.Frame(row, bg=bar, width=4).pack(side="left", fill="y")
            labels = tk.Frame(row, bg="white")
            labels.pack(side="left", padx=(10, 24), pady=4)
            self.label(labels, account_label(account), font=(FONT, 10, "bold")).pack(anchor="w")
            status_color = COLOR_ERROR if failing else COLOR_MUTED
            self.label(labels, self.app.account_status(account), font=(FONT, 9), fg=status_color).pack(anchor="w")
            # Packed from the right edge: the first one ends up rightmost.
            actions = [
                (t("remove"), partial(self.remove, account)),
                (t("edit"), partial(self.app.edit_account, account)),
            ]
            if account["type"] == "caldav":
                actions.append((t("calendars_button"), partial(self.app.ask_calendars, account["id"])))
            for text, command in actions:
                button(row, text, command, "link").pack(side="right", padx=(4, 0))

    def remove(self, account):
        if messagebox.askyesno("Calendar Alarm", t("remove_question", name=account_label(account)), parent=self):
            self.app.remove_account(account)


class SettingsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "settings", t("title_settings"), t("settings_intro"))
        config = app.config
        self.grid = tk.Frame(self.frame, bg="white")
        self.grid.pack(anchor="w")
        self.next_row = 0

        self.section(t("section_alarm"))
        self.lead = self.number(t("lead_label"), config["lead_minutes"], 0, 120, t("unit_minutes"))
        self.snooze = self.number(t("snooze_label"), config["snooze_minutes"], 1, 60, t("unit_minutes"))
        self.sounds = alarm_sounds()
        # Compared the way Windows does: C:\Windows and C:\WINDOWS are the same folder.
        current = os.path.normcase(config["sound"])
        if current not in map(os.path.normcase, self.sounds.values()):
            self.sounds[Path(config["sound"]).name] = config["sound"]
        chosen = next(name for name, path in self.sounds.items() if os.path.normcase(path) == current)
        extras = tk.Frame(self.grid, bg="white")
        self.sound = self.choice(t("sound_label"), list(self.sounds), chosen, extras)
        button(extras, t("sound_preview"), self.preview, "link").pack(side="left")
        button(extras, t("sound_browse"), self.browse, "link").pack(side="left")
        silences = [t("never") if minutes == 0 else t("minutes_value", minutes=minutes) for minutes in SILENCE_CHOICES]
        silence = SILENCE_CHOICES.index(config["silence_after_minutes"]) if config["silence_after_minutes"] in SILENCE_CHOICES else 0
        self.silence = self.choice(t("silence_label"), silences, silences[silence])
        self.ignore_free = self.checkbox(t("ignore_free"), config["ignore_free"])

        self.section(t("section_agenda"))
        self.days = self.number(t("agenda_days_label"), config["agenda_days"], 1, 14, t("unit_days"))
        self.show_all_day = self.checkbox(t("show_all_day"), config["show_all_day"])

        self.section(t("section_general"))
        self.languages = [None, *texts.LANGUAGES]
        languages = [t("language_auto"), *texts.LANGUAGES.values()]
        self.language = self.choice(t("language"), languages, languages[self.languages.index(config["language"])])
        polls = [t("minutes_value", minutes=seconds // 60) for seconds in POLL_CHOICES]
        poll = POLL_CHOICES.index(config["poll_seconds"]) if config["poll_seconds"] in POLL_CHOICES else 0
        self.poll = self.choice(t("poll_label"), polls, polls[poll])
        self.autostart = self.checkbox(t("autostart"), autostart_command() is not None)

        self.label(self.footer, f"Calendar Alarm {__version__}", font=(FONT, 9), fg=COLOR_MUTED).pack(side="left")
        button(self.footer, t("cancel"), self.close).pack(side="right")
        button(self.footer, t("save"), self.submit, "primary").pack(side="right", padx=(0, 8))

    # --- building blocks: one setting per row of the grid

    def place(self, label, control, extra=None):
        if label:
            self.label(self.grid, label).grid(row=self.next_row, column=0, sticky="w", padx=(0, 18), pady=5)
        control.grid(row=self.next_row, column=1, sticky="w", pady=5, columnspan=1 if label else 3)
        if extra is not None:
            extra.grid(row=self.next_row, column=2, sticky="w", padx=(8, 0))
        self.next_row += 1

    def section(self, title):
        top = 0 if self.next_row == 0 else 14
        self.label(self.grid, title, font=(FONT, 10, "bold"), fg=COLOR_ACCENT).grid(
            row=self.next_row, column=0, columnspan=3, sticky="w", pady=(top, 2)
        )
        self.next_row += 1

    def number(self, label, value, low, high, unit):
        variable = tk.StringVar(self, value=str(value))
        cell = tk.Frame(self.grid, bg="white")
        tk.Spinbox(
            cell, from_=low, to=high, width=5, font=(FONT, 10), textvariable=variable, relief="flat",
            highlightthickness=1, highlightbackground="#c4c7cc", highlightcolor=COLOR_ACCENT,
        ).pack(side="left")
        self.label(cell, unit, fg=COLOR_MUTED).pack(side="left", padx=(8, 0))
        self.place(label, cell)

        def read():
            try:
                return min(high, max(low, int(variable.get())))
            except ValueError:
                return value

        return read

    def choice(self, label, values, selected, extra=None):
        combobox = ttk.Combobox(self.grid, state="readonly", values=values, width=28, font=(FONT, 10))
        combobox.set(selected)
        self.place(label, combobox, extra)
        return combobox

    def checkbox(self, label, checked):
        variable = tk.BooleanVar(self, value=checked)
        control = tk.Checkbutton(
            self.grid, text=label, variable=variable, font=(FONT, 10), bg="white", activebackground="white", fg=COLOR_TEXT
        )
        control.grid(row=self.next_row, column=0, columnspan=3, sticky="w", pady=3)
        self.next_row += 1
        return variable

    # --- sound

    def preview(self):
        # Never over a real alarm: stopping the preview would silence it.
        if not self.app.sound_playing:
            winsound.PlaySound(self.sounds[self.sound.get()], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def browse(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[(t("wav_files"), "*.wav")])
        if path:
            path = str(Path(path))
            self.sounds[Path(path).name] = path
            self.sound.config(values=list(self.sounds))
            self.sound.set(Path(path).name)

    # --- saving

    def submit(self):
        settings = {
            "lead_minutes": self.lead(),
            "snooze_minutes": self.snooze(),
            "sound": self.sounds[self.sound.get()],
            "silence_after_minutes": SILENCE_CHOICES[self.silence.current()],
            "ignore_free": self.ignore_free.get(),
            "agenda_days": self.days(),
            "show_all_day": self.show_all_day.get(),
            "language": self.languages[self.language.current()],
            "poll_seconds": POLL_CHOICES[self.poll.current()],
        }
        autostart = self.autostart.get()
        self.close()
        self.app.apply_settings(settings, autostart)

    def close(self):
        if not self.app.sound_playing:
            winsound.PlaySound(None, winsound.SND_PURGE)
        super().close()


class AlarmWindow(tk.Toplevel):
    def __init__(self, app, event, index):
        super().__init__(app.root)
        self.app = app
        self.event = event
        self.title(t("alarm_title"))
        self.configure(bg="white")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        # Closing the window must not silence the alarm for good.
        self.protocol("WM_DELETE_WINDOW", self.snooze)

        tk.Frame(self, bg=COLOR_ERROR, height=6).pack(fill="x")
        footer = tk.Frame(self, bg=COLOR_PANEL, padx=26, pady=12)
        footer.pack(side="bottom", fill="x")
        frame = tk.Frame(self, bg="white", padx=26, pady=20)
        frame.pack(fill="both", expand=True)
        self.countdown = tk.Label(frame, font=(FONT, 12, "bold"), bg="white", fg=COLOR_ERROR)
        self.countdown.pack(anchor="w")
        tk.Label(
            frame, text=event.summary, font=(FONT, 20, "bold"), bg="white", fg=COLOR_TEXT, wraplength=520, justify="left"
        ).pack(anchor="w", pady=(6, 8))
        details = f"{event.start:%H:%M} – {event.end:%H:%M}   ·   {event.calendar}"
        tk.Label(frame, text=details, font=(FONT, 11), bg="white", fg=COLOR_MUTED).pack(anchor="w")
        if event.location:
            tk.Label(
                frame, text=event.location, font=(FONT, 11), bg="white", fg=COLOR_MUTED, wraplength=520, justify="left"
            ).pack(anchor="w")
        # takefocus=0: a key typed elsewhere when the window pops up must not press a button.
        snooze_text = t("snooze", minutes=app.config["snooze_minutes"])
        button(footer, snooze_text, self.snooze, takefocus=0, width=14).pack(side="right")
        button(footer, t("stop"), self.stop, "primary", takefocus=0, width=14).pack(side="right", padx=(0, 10))

        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2 + 30 * index
        y = (self.winfo_screenheight() - self.winfo_height()) // 3 + 30 * index
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.refresh()

    def refresh(self):
        minutes = round((self.event.start - datetime.now().astimezone()).total_seconds() / 60)
        if minutes > 0:
            text = t("in_minutes", minutes=minutes)
        elif minutes == 0:
            text = t("now")
        else:
            text = t("started_since", minutes=-minutes)
        self.countdown.config(text=text)
        self.timer = self.after(1000, self.refresh)

    def destroy(self):
        self.after_cancel(self.timer)
        super().destroy()

    def stop(self):
        self.app.stop_alarm(self.event)

    def snooze(self):
        self.app.snooze_alarm(self.event)


class AgendaWindow(tk.Toplevel):
    """Every event of every watched calendar for the coming days, grouped by day."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title(f"Calendar Alarm — {t('agenda_title')}")
        self.configure(bg="white")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _: self.close())
        self.bind("<MouseWheel>", self.scroll)
        # Sized in characters, so that the window keeps its proportions on a scaled display.
        unit = tkfont.Font(family=FONT, size=10).measure("0")
        self.wrap = 38 * unit
        width, height = 62 * unit, min(100 * unit, self.winfo_screenheight() - 160)
        x = (self.winfo_screenwidth() - width) // 2
        self.geometry(f"{width}x{height}+{x}+60")
        self.minsize(width, 30 * unit)
        self.resizable(False, True)

        header = tk.Frame(self, bg="white", padx=22, pady=14)
        header.pack(fill="x")
        tk.Label(header, text=t("agenda_title"), font=(FONT, 17, "bold"), bg="white", fg=COLOR_TEXT).pack(anchor="w")
        self.subtitle = tk.Label(header, font=(FONT, 9), bg="white", fg=COLOR_MUTED)
        self.subtitle.pack(anchor="w")
        tk.Frame(self, bg=COLOR_LINE, height=1).pack(fill="x")

        self.legend = tk.Frame(self, bg=COLOR_PANEL, padx=22, pady=8)
        self.legend.pack(side="bottom", fill="x")
        scrollbar = tk.Scrollbar(self, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(self, bg="white", highlightthickness=0, yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.canvas.yview)
        self.days = tk.Frame(self.canvas, bg="white", padx=22)
        inner = self.canvas.create_window(0, 0, window=self.days, anchor="nw")
        self.days.bind("<Configure>", lambda _: self.canvas.config(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(inner, width=event.width))

        self.timer = None
        self.render()

    def scroll(self, event):
        if self.days.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-event.delta // 120, "units")

    def render(self):
        """Redraws everything; called when the events change and regularly, as time passes."""
        if self.timer is not None:
            self.after_cancel(self.timer)
        self.timer = self.after(30_000, self.render)
        app = self.app
        now = datetime.now().astimezone()
        days = app.config["agenda_days"]
        self.subtitle.config(text=f"{t('agenda_subtitle', days=days)}  ·  {app.status_text()}")
        for frame in (self.days, self.legend):
            for child in frame.winfo_children():
                child.destroy()

        calendars = app.watched_calendars()
        colors = {name: CALENDAR_COLORS[index % len(CALENDAR_COLORS)] for index, name in enumerate(calendars)}
        if not app.config["accounts"]:
            tk.Label(self.days, text=t("agenda_no_account"), font=(FONT, 10), bg="white", fg=COLOR_MUTED).pack(
                anchor="w", pady=(18, 8)
            )
            button(self.days, t("menu_accounts"), app.show_accounts, "primary").pack(anchor="w")
            return

        events = [event for event in app.events.values() if app.config["show_all_day"] or not event.all_day]
        upcoming = [event for event in events if not event.all_day and event.start > now]
        next_event = min(upcoming, key=lambda event: event.start, default=None)
        for offset, (day, day_events) in enumerate(agenda_days(events, now.date(), days)):
            name = texts.day_name(day)
            title = f"{t('today')}  ·  {name}" if offset == 0 else f"{t('tomorrow')}  ·  {name}" if offset == 1 else name
            tk.Label(
                self.days,
                text=title[0].upper() + title[1:],
                font=(FONT, 11, "bold"),
                bg="white",
                fg=COLOR_ACCENT if offset == 0 else COLOR_TEXT,
            ).pack(anchor="w", pady=(16, 4))
            if not day_events:
                tk.Label(self.days, text=t("nothing_planned"), font=(FONT, 9), bg="white", fg=COLOR_MUTED).pack(anchor="w")
            for event in day_events:
                self.row(event, now, colors.get(event.calendar, COLOR_MUTED), event is next_event)
        tk.Frame(self.days, bg="white", height=16).pack()

        for index, name in enumerate(calendars):
            entry = tk.Frame(self.legend, bg=self.legend["bg"])
            entry.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18), pady=1)
            tk.Frame(entry, bg=colors[name], width=10, height=10).pack(side="left", padx=(0, 6))
            tk.Label(entry, text=name[:32], font=(FONT, 9), bg=self.legend["bg"], fg=COLOR_TEXT).pack(side="left")

    def row(self, event, now, color, is_next):
        ongoing = not event.all_day and event.start <= now < event.end
        past = not event.all_day and event.end <= now
        background = COLOR_HIGHLIGHT if ongoing or is_next else "white"
        text_color = COLOR_MUTED if past else COLOR_TEXT

        row = tk.Frame(self.days, bg=background)
        row.pack(fill="x", pady=2)
        tk.Frame(row, bg=COLOR_LINE if past else color, width=4).pack(side="left", fill="y")
        hours = tk.Frame(row, bg=background)
        hours.pack(side="left", anchor="n", padx=(10, 12), pady=6)
        if event.all_day:
            tk.Label(hours, text=t("all_day"), width=6, anchor="w", font=(FONT, 10), bg=background, fg=text_color).pack()
        else:
            tk.Label(
                hours, text=f"{event.start:%H:%M}", width=6, anchor="w", font=(FONT, 10, "bold"), bg=background, fg=text_color
            ).pack()
            tk.Label(
                hours, text=f"{event.end:%H:%M}", width=6, anchor="w", font=(FONT, 9), bg=background, fg=COLOR_MUTED
            ).pack(anchor="w")

        body = tk.Frame(row, bg=background)
        body.pack(side="left", fill="x", expand=True, pady=6)
        tk.Label(
            body, text=event.summary, font=(FONT, 10, "bold"), bg=background, fg=text_color, wraplength=self.wrap, justify="left"
        ).pack(anchor="w")
        details = "  ·  ".join(part for part in (event.location, event.calendar) if part)
        tk.Label(
            body, text=details, font=(FONT, 9), bg=background, fg=COLOR_MUTED, wraplength=self.wrap, justify="left"
        ).pack(anchor="w")
        if ongoing or is_next:
            tk.Label(
                body, text=self.badge(event, now, ongoing), font=(FONT, 9, "bold"), bg=background, fg=COLOR_ACCENT
            ).pack(anchor="w")

    def badge(self, event, now, ongoing):
        if ongoing:
            return t("ongoing")
        minutes = max(1, round((event.start - now).total_seconds() / 60))
        return t("in_min", minutes=minutes) if minutes < 90 else t("in_hours", hours=round(minutes / 60))

    def destroy(self):
        self.after_cancel(self.timer)
        super().destroy()

    def close(self):
        self.app.dialogs.pop("agenda", None)
        self.destroy()


def make_icon(color):
    # Drawn large: the same picture also makes the icon of the executable (build.ps1).
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 32, 232, 232), 36, fill=color)
    draw.rounded_rectangle((44, 96, 212, 212), 16, fill="white")
    draw.ellipse((96, 120, 160, 184), fill=color)
    return image


# --- application ------------------------------------------------------------


class App:
    def __init__(self):
        self.config = load_config()
        texts.set_language(self.config["language"])
        # key -> ISO start, for the alarms stopped by the user.
        self.acked = load_json(STATE_PATH, {}).get("acked", {})
        # account id -> {key: event}, and the merged view the alarms run on
        self.account_events = {}
        self.events = {}
        # key -> (ring again at, event)
        self.snoozed = {}
        self.windows = {}
        self.dialogs = {}
        self.inbox = queue.SimpleQueue()
        # Per account id: last successful sync, last error, calendars of a CalDAV account.
        self.last_sync = {}
        self.sync_errors = {}
        self.calendar_names = {}
        self.owner_names = {}
        self.healthy = None
        # When each account started being watched (program start, or the moment it was added).
        self.started = datetime.now().astimezone()
        self.watched_since = {}
        self.warned = False
        self.sound_playing = False
        self.last_tick = time.time()

        self.root = tk.Tk()
        self.root.withdraw()
        self.icons = {True: make_icon(COLOR_OK), False: make_icon(COLOR_ERROR)}
        # Kept on self: Tk only holds a weak reference to the picture of its windows.
        self.window_icon = ImageTk.PhotoImage(self.icons[True].resize((64, 64)))
        self.root.iconphoto(True, self.window_icon)
        self.tray = pystray.Icon(APP, self.icons[False], "Calendar Alarm", self.build_menu())
        self.fetcher = Fetcher(self)

    # --- plumbing -------------------------------------------------------

    def post(self, action):
        """Runs action on the UI thread (callable from any thread)."""
        self.inbox.put(action)

    def build_menu(self):
        # Texts are looked up when the menu opens, so that it follows a change of language.
        def item(key, action):
            return pystray.MenuItem(lambda _: t(key), lambda: self.post(action))

        return pystray.Menu(
            pystray.MenuItem(lambda _: self.status_text(), None, enabled=False),
            pystray.MenuItem(lambda _: self.next_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            # Hidden: it gives a plain click on the icon its action, without a bold entry in the menu.
            pystray.MenuItem("agenda", lambda: self.post(self.show_agenda), default=True, visible=False),
            item("menu_agenda", self.show_agenda),
            item("sync_now", self.fetcher_wake),
            item("test_alarm", self.test_alarm),
            item("menu_accounts", self.show_accounts),
            item("menu_settings", self.show_settings),
            item("open_log", lambda: os.startfile(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            item("quit", self.quit),
        )

    def run(self):
        # Registered on the very first run, then kept pointing at this copy if the program was moved.
        registered = autostart_command()
        if registered != launch_command() and (registered is not None or not CONFIG_PATH.exists()):
            set_autostart(True)
        self.tray.run_detached()
        self.fetcher.start()
        if not self.config["accounts"]:
            self.show_accounts()
        self.root.after(TICK_MS, self.tick)
        self.root.mainloop()

    def quit(self):
        self.set_sound(False)
        self.tray.stop()
        self.root.destroy()

    def fetcher_wake(self):
        self.fetcher.wake.set()

    def set_language(self, code):
        self.config["language"] = code
        texts.set_language(code)
        # Open windows were written in the previous language.
        for dialog in list(self.dialogs.values()):
            dialog.close()
        self.save_config()

    def show_settings(self):
        self.open("settings", lambda: SettingsDialog(self))

    def apply_settings(self, settings, autostart):
        language = settings.pop("language")
        self.config.update(settings)
        if autostart != (autostart_command() is not None):
            set_autostart(autostart)
        log.info("settings saved: %s", settings)
        if language != self.config["language"]:
            self.set_language(language)
        else:
            self.save_config()

    # --- accounts -------------------------------------------------------

    def account(self, account_id):
        return next((account for account in self.config["accounts"] if account["id"] == account_id), None)

    def save_config(self):
        stored = {key: value for key, value in self.config.items() if value != DEFAULT_CONFIG.get(key)}
        save_json(CONFIG_PATH, stored)
        self.fetcher.reset()
        self.refresh_accounts()

    def add_account(self, account, secret):
        account["id"] = uuid.uuid4().hex[:8]
        set_secret(account, secret)
        self.watched_since[account["id"]] = datetime.now().astimezone()
        self.config["accounts"].append(account)
        log.info("account added: %s", account_label(account))

    def save_caldav_account(self, account, provider, url, username, password):
        if account is None:
            new = {"type": "caldav", "provider": provider["key"], "url": url, "username": username, "calendars": None}
            self.add_account(new, password)
        else:
            if (url, username) != (account["url"], account["username"]):
                # Another account: its calendars have to be chosen again.
                account["calendars"] = None
                self.calendar_names.pop(account["id"], None)
            account.update(url=url, username=username)
            set_secret(account, password)
        self.save_config()

    def save_ics_account(self, account, provider, name, url):
        if account is None:
            self.add_account({"type": "ics", "provider": provider["key"], "name": name}, url)
        else:
            account["name"] = name
            set_secret(account, url)
        self.save_config()

    def save_calendars(self, account, urls):
        account["calendars"] = urls
        names = self.calendar_names.get(account["id"], {})
        log.info("%s: watching %s", account_label(account), [names.get(url, url) for url in urls])
        self.save_config()

    def remove_account(self, account):
        self.config["accounts"].remove(account)
        delete_secret(account)
        for per_account in (
            self.account_events,
            self.last_sync,
            self.sync_errors,
            self.calendar_names,
            self.owner_names,
            self.watched_since,
        ):
            per_account.pop(account["id"], None)
        log.info("account removed: %s", account_label(account))
        self.merge_events()
        self.save_config()

    def open(self, name, factory):
        if name not in self.dialogs:
            self.dialogs[name] = factory()
        else:
            self.dialogs[name].lift()
            self.dialogs[name].focus_force()

    def show_agenda(self):
        self.open("agenda", lambda: AgendaWindow(self))

    def watched_calendars(self):
        """Names of the calendars being watched, in a stable order."""
        names = []
        for account in self.config["accounts"]:
            if account["type"] == "ics":
                names.append(account["name"])
            else:
                known = self.calendar_names.get(account["id"], {})
                names.extend(known.get(url, url) for url in account["calendars"] or [])
        return names

    def show_accounts(self):
        self.open("accounts", lambda: AccountsDialog(self))

    def refresh_accounts(self):
        if "accounts" in self.dialogs:
            self.dialogs["accounts"].refresh()
        if "agenda" in self.dialogs:
            self.dialogs["agenda"].render()
        self.refresh_tray()

    def edit_account(self, account, provider=None, problem=None):
        """Opens the window of a new account of this provider, or of an existing account."""
        provider = provider or provider_of(account)
        message = t(f"help_{provider['key']}")
        if problem:
            message = f"{problem}\n\n{message}"
        dialog = CaldavDialog if provider["kind"] == "caldav" else IcsDialog
        self.open(provider["kind"], lambda: dialog(self, account, provider, message))

    def ask_calendars(self, account_id):
        account = self.account(account_id)
        if account is not None and self.calendar_names.get(account_id):
            self.open("calendars", lambda: CalendarsDialog(self, account))

    def account_problem(self, account):
        """Why the alarms of this account cannot be trusted right now, or None."""
        account_id = account["id"]
        if account_id in self.sync_errors:
            return t("failing", error=self.sync_errors[account_id])[:90]
        if account["type"] == "caldav" and account["calendars"] is None:
            return t("none_chosen")
        if account_id not in self.last_sync:
            return t("waiting")
        return None

    def account_status(self, account):
        return self.account_problem(account) or t("synced_at", time=f"{self.last_sync[account['id']]:%H:%M:%S}")

    # --- fetcher callbacks ----------------------------------------------

    def on_discovery(self, account_id, calendar_names, owner_name):
        self.calendar_names[account_id] = calendar_names
        self.owner_names[account_id] = owner_name

    def merge_events(self):
        self.events = {key: event for events in self.account_events.values() for key, event in events.items()}

    def on_events(self, account_id, events):
        account = self.account(account_id)
        if account is None:
            return
        now = datetime.now().astimezone()
        if account_id not in self.last_sync or account_id in self.sync_errors:
            log.info(
                "%s: sync ok, %d events in the next %d days",
                account_label(account), len(events), self.config["agenda_days"],
            )
        self.account_events[account_id] = {event.key: event for event in events}
        self.last_sync[account_id] = now
        self.sync_errors.pop(account_id, None)
        self.merge_events()

        # An upcoming event that left the calendars was cancelled or moved: forget its alarm.
        def gone(event):
            return event.key not in self.events and event.start > now and not event.key.startswith(TEST_PREFIX)

        for key in [key for key, (_, event) in self.snoozed.items() if gone(event)]:
            del self.snoozed[key]
        for key in [key for key, window in self.windows.items() if gone(window.event)]:
            self.close_window(key)

        limit = (now - timedelta(days=2)).isoformat()
        pruned = {key: start for key, start in self.acked.items() if start > limit}
        if pruned != self.acked:
            self.acked = pruned
            self.save_state()
        self.refresh_accounts()

    def on_sync_error(self, account_id, message):
        self.sync_errors[account_id] = message
        self.refresh_accounts()

    def on_refused(self, account_id):
        account = self.account(account_id)
        if account is None:
            return
        self.sync_errors[account_id] = t("refused_short")
        self.refresh_accounts()
        self.edit_account(account, problem=t(f"refused_{account['type']}", name=account_label(account)))

    # --- alarms ---------------------------------------------------------

    def tick(self):
        while True:
            try:
                action = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception:
                log.exception("UI action failed")

        # A jump of the wall clock means the PC just woke up: resync at once.
        if time.time() - self.last_tick > 30:
            log.info("clock jump detected, resyncing")
            self.fetcher_wake()
        self.last_tick = time.time()

        now = datetime.now().astimezone()
        handled = self.acked.keys() | self.snoozed.keys() | self.windows.keys()
        lead = timedelta(minutes=self.config["lead_minutes"])
        grace = timedelta(minutes=self.config["late_grace_minutes"])
        ringable = [event for event in self.events.values() if not (event.free and self.config["ignore_free"])]
        for event in due_events(ringable, handled, now, lead, grace):
            self.ring(event)
        for key, (ring_at, event) in list(self.snoozed.items()):
            if now >= ring_at:
                del self.snoozed[key]
                self.ring(event)

        silence_after = self.config["silence_after_minutes"]
        if self.sound_playing and silence_after and now - self.ringing_since >= timedelta(minutes=silence_after):
            # Nobody is there: the windows stay, the noise stops.
            self.set_sound(False)

        self.refresh_health()
        self.root.after(TICK_MS, self.tick)

    def ring(self, event):
        log.info("alarm: %s at %s", event.summary, event.start.isoformat())
        self.windows[event.key] = AlarmWindow(self, event, len(self.windows))
        self.ringing_since = datetime.now().astimezone()
        self.set_sound(True)

    def close_window(self, key):
        window = self.windows.pop(key, None)
        if window is not None:
            window.destroy()
        if not self.windows:
            self.set_sound(False)

    def stop_alarm(self, event):
        self.close_window(event.key)
        if not event.key.startswith(TEST_PREFIX):
            self.acked[event.key] = event.start.isoformat()
            self.save_state()

    def snooze_alarm(self, event):
        self.close_window(event.key)
        ring_at = datetime.now().astimezone() + timedelta(minutes=self.config["snooze_minutes"])
        self.snoozed[event.key] = (ring_at, event)

    def test_alarm(self):
        now = datetime.now().astimezone()
        start = now + timedelta(minutes=self.config["lead_minutes"])
        key = f"{TEST_PREFIX}{now.isoformat()}"
        self.ring(Event(key, start, start + timedelta(hours=1), t("test_event"), "", t("test_calendar")))

    def set_sound(self, playing):
        if playing == self.sound_playing:
            return
        self.sound_playing = playing
        if not playing:
            winsound.PlaySound(None, winsound.SND_PURGE)
            return
        flags = winsound.SND_ASYNC | winsound.SND_LOOP
        if os.path.exists(self.config["sound"]):
            winsound.PlaySound(self.config["sound"], flags | winsound.SND_FILENAME)
        else:
            winsound.PlaySound("SystemExclamation", flags | winsound.SND_ALIAS)

    def save_state(self):
        save_json(STATE_PATH, {"acked": self.acked})

    # --- tray -----------------------------------------------------------

    def stale_accounts(self, since_watched=False):
        """Accounts without a recent successful sync.

        since_watched leaves an account that never synced the same delay, counted from the moment it
        started being watched: a calendar added two seconds ago is not a broken one.
        """
        now = datetime.now().astimezone()
        stale_after = timedelta(seconds=max(3 * self.config["poll_seconds"], 300))
        stale = []
        for account in self.config["accounts"]:
            reference = self.last_sync.get(account["id"])
            if reference is None and since_watched:
                reference = self.watched_since.get(account["id"], self.started)
            if reference is None or now - reference >= stale_after:
                stale.append(account)
        return stale

    def is_healthy(self):
        return bool(self.config["accounts"]) and not self.stale_accounts()

    def status_text(self):
        accounts = self.config["accounts"]
        if not accounts:
            return t("not_configured")
        for account in accounts:
            problem = self.account_problem(account)
            if problem:
                return t("account_state", name=account_label(account), state=problem)[:120]
        return t("last_sync", time=f"{min(self.last_sync[account['id']] for account in accounts):%H:%M:%S}")

    def next_text(self):
        now = datetime.now().astimezone()
        upcoming = [
            event
            for event in self.events.values()
            if not event.all_day and now < event.start <= now + timedelta(hours=24)
        ]
        if not upcoming:
            return t("no_upcoming")
        first = min(upcoming, key=lambda event: event.start)
        return t("next_event", time=f"{first.start:%H:%M}", title=first.summary)[:80]

    def refresh_tray(self):
        self.tray.title = f"Calendar Alarm — {self.status_text()}"[:120]
        self.tray.update_menu()

    def refresh_health(self):
        healthy = self.is_healthy()
        if healthy != self.healthy:
            self.healthy = healthy
            self.tray.icon = self.icons[healthy]
            self.refresh_tray()
        # The icon turns red at once; the notification waits until an account is really overdue.
        overdue = self.stale_accounts(since_watched=True)
        if overdue and not self.warned:
            names = ", ".join(account_label(account) for account in overdue)
            log.warning("no successful sync recently: %s", names)
            self.tray.notify(t("stale_notification", names=names)[:250], "Calendar Alarm")
        self.warned = bool(overdue)


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def log_uncaught(exc_type, exc, traceback):
        log.critical("uncaught exception", exc_info=(exc_type, exc, traceback))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def check():
    """--check: reads every account once, without any window. Returns 0 when all of them answer."""
    app = types.SimpleNamespace(config=load_config(), post=lambda action: None)
    fetcher = Fetcher(app)
    ok = bool(app.config["accounts"])
    for account in app.config["accounts"]:
        try:
            events = fetcher.fetch(account, get_secret(account))
            log.info("check: %s: %d events", account_label(account), len(events))
        except Exception as exc:
            ok = False
            log.error("check: %s: %r", account_label(account), exc)
    return 0 if ok else 1


def forget():
    """--forget: removes every saved secret (run by the uninstaller)."""
    for account in load_config()["accounts"]:
        delete_secret(account)


def main():
    setup_logging()
    if "--forget" in sys.argv:
        return forget()
    if "--check" in sys.argv:
        sys.exit(check())
    # The handle is kept for the lifetime of the process: only one instance may run.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP}")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log.info("already running, exiting")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    log.info("starting %s", __version__)
    app = App()
    app.root.report_callback_exception = lambda *exc_info: log.error("Tk callback failed", exc_info=exc_info)
    app.run()
    del mutex


if __name__ == "__main__":
    main()
