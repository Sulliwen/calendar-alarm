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
import types
import urllib.error
import urllib.request
import uuid
import winreg
import winsound
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tkinter import messagebox
from urllib.parse import unquote, urlsplit

import caldav
import icalendar
import keyring
import keyring.errors
import pystray
import recurring_ical_events
from caldav.lib.error import AuthorizationError
from PIL import Image, ImageDraw, ImageTk

APP = "calendar-alarm"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "calendar-alarm.log"

INFOMANIAK_URL = "https://sync.infomaniak.com"
DEFAULT_CONFIG = {
    # CalDAV: {"id", "type": "caldav", "url", "username", "calendars": [urls] or None until chosen}
    # iCal:   {"id", "type": "ics", "name"}
    # Passwords and iCal addresses live in the Windows Credential Manager, not here.
    "accounts": [],
    "lead_minutes": 10,
    "snooze_minutes": 5,
    # An event found after it started still rings during this many minutes.
    "late_grace_minutes": 5,
    "poll_seconds": 60,
    "sound": r"C:\Windows\Media\Alarm01.wav",
    # Extra addresses used to recognise the invitations you declined.
    "my_emails": [],
}

HORIZON = timedelta(hours=24)
RETRY_SECONDS = 15
TICK_MS = 500
REDISCOVER_SECONDS = 1800
TEST_PREFIX = "test:"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CalendarAlarm"
COLOR_OK = "#2e7d32"
COLOR_ERROR = "#c62828"
FONT = "Segoe UI"
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
        # Version 1.0 knew a single Infomaniak account, kept at the top level.
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
    """Timed, non-cancelled, non-declined events among (already expanded) VEVENT components."""
    events = []
    for component in components:
        dtstart = component.get("DTSTART")
        # All-day events carry a date, not a datetime.
        if dtstart is None or not isinstance(dtstart.dt, datetime):
            continue
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if is_declined(component, my_addresses):
            continue
        # astimezone() reads a naive (floating) datetime as local time.
        start = dtstart.dt.astimezone()
        dtend = component.get("DTEND")
        if dtend is not None and isinstance(dtend.dt, datetime):
            end = dtend.dt.astimezone()
        elif component.get("DURATION") is not None:
            end = start + component.get("DURATION").dt
        else:
            end = start
        events.append(
            Event(
                # No account in the key: a meeting present in two calendars rings once.
                key=f"{component.get('UID')}|{start.isoformat()}",
                start=start,
                end=end,
                summary=str(component.get("SUMMARY") or "(sans titre)"),
                location=str(component.get("LOCATION") or ""),
                calendar=calendar_name,
            )
        )
    return events


def due_events(events, handled, now, lead, grace):
    """Events whose alarm window is open and that were neither stopped, snoozed nor ringing."""
    return [
        event
        for event in events
        if event.key not in handled and event.start - lead <= now < event.start + grace
    ]


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
        start = now - timedelta(minutes=self.app.config["late_grace_minutes"])
        if account["type"] == "ics":
            return self.fetch_ics(account, secret, start, now + HORIZON)
        return self.fetch_caldav(account, secret, start, now + HORIZON)

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


class Dialog(tk.Toplevel):
    """A settings window, open at most once per name."""

    def __init__(self, app, name, title):
        super().__init__(app.root)
        self.app = app
        self.name = name
        self.title(f"Calendar Alarm — {title}")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.frame = tk.Frame(self, padx=20, pady=16)
        self.frame.pack()

    def text(self, message):
        tk.Label(self.frame, text=message, font=(FONT, 10), wraplength=440, justify="left").pack(anchor="w", pady=(0, 10))

    def field(self, grid, row, label, value, show=""):
        tk.Label(grid, text=label, font=(FONT, 10)).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
        entry = tk.Entry(grid, font=(FONT, 10), width=38, show=show)
        entry.insert(0, value)
        entry.grid(row=row, column=1, pady=3)
        return entry

    def buttons(self, submit_text, submit):
        self.status = tk.Label(self.frame, text="", font=(FONT, 9), fg=COLOR_ERROR, wraplength=440, justify="left")
        self.status.pack(anchor="w", pady=(8, 0))
        row = tk.Frame(self.frame)
        row.pack(anchor="e", pady=(8, 0))
        self.submit_button = tk.Button(row, text=submit_text, font=(FONT, 10), command=submit)
        self.submit_button.pack(side="left", padx=(0, 8))
        tk.Button(row, text="Annuler", font=(FONT, 10), command=self.close).pack(side="left")

    def test(self, check, *args):
        """Runs check(*args) off the UI thread, then save(*args) unless it returned an error message."""
        self.submit_button.config(state="disabled")
        self.status.config(text="Test de la connexion…", fg="black")

        def run():
            try:
                error = check(*args)
            except Refused:
                error = "Refusé par le serveur : vérifie la saisie."
            except Exception as exc:
                error = f"Connexion impossible : {exc or type(exc).__name__}"
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
    def __init__(self, app, account, message):
        super().__init__(app, "caldav", "compte CalDAV")
        self.account = account
        self.text(message)
        grid = tk.Frame(self.frame)
        grid.pack(anchor="w")
        self.url = self.field(grid, 0, "Serveur", account["url"] if account else INFOMANIAK_URL)
        self.username = self.field(grid, 1, "Identifiant (ex. AB12345)", account["username"] if account else "")
        self.password = self.field(grid, 2, "Mot de passe d'application", "", show="•")
        self.buttons("Tester et enregistrer", self.submit)
        self.bind("<Return>", lambda _: self.submit())
        (self.password if account else self.username).focus_set()

    def submit(self):
        url, username, password = self.url.get().strip(), self.username.get().strip(), self.password.get()
        if not (url and username and password):
            self.status.config(text="Les trois champs sont obligatoires.")
            return
        self.test(self.check, url, username, password)

    def check(self, url, username, password):
        if not discover(url, username, password, self.app.config).calendars:
            return "Connexion réussie, mais aucun agenda trouvé."

    def save(self, url, username, password):
        self.app.save_caldav_account(self.account, url, username, password)


class IcsDialog(Dialog):
    def __init__(self, app, account, message):
        super().__init__(app, "ics", "adresse iCal")
        self.account = account
        self.text(message)
        grid = tk.Frame(self.frame)
        grid.pack(anchor="w")
        self.label = self.field(grid, 0, "Nom de l'agenda", account["name"] if account else "Google")
        self.url = self.field(grid, 1, "Adresse secrète iCal", "")
        self.buttons("Tester et enregistrer", self.submit)
        self.bind("<Return>", lambda _: self.submit())
        self.url.focus_set()

    def submit(self):
        name, url = self.label.get().strip(), normalize_ics_url(self.url.get())
        if not name or url is None:
            self.status.config(text="Il faut un nom et une adresse commençant par https://.")
            return
        self.test(self.check, name, url)

    def check(self, name, url):
        try:
            icalendar.Calendar.from_ical(download_ics(url))
        except ValueError:
            return "Cette adresse ne renvoie pas un agenda au format iCal."

    def save(self, name, url):
        self.app.save_ics_account(self.account, name, url)


class CalendarsDialog(Dialog):
    def __init__(self, app, account):
        super().__init__(app, "calendars", "agendas surveillés")
        self.account = account
        self.text(f"{account_label(account)}\nCoche les agendas dont les rendez-vous doivent sonner.")
        names = app.calendar_names[account["id"]]
        if account["calendars"] is None:
            # First choice: propose the calendar named after the account owner.
            selected = {url for url, name in names.items() if name == app.owner_names.get(account["id"])}
        else:
            selected = set(account["calendars"])
        self.choices = {}
        for url, name in sorted(names.items(), key=lambda item: item[1].lower()):
            self.choices[url] = tk.BooleanVar(self, value=url in selected)
            tk.Checkbutton(self.frame, text=name, variable=self.choices[url], font=(FONT, 10)).pack(anchor="w")
        self.buttons("Enregistrer", self.submit)

    def submit(self):
        urls = [url for url, checked in self.choices.items() if checked.get()]
        if not urls:
            self.status.config(text="Coche au moins un agenda.")
            return
        self.app.save_calendars(self.account, urls)
        self.close()


class AccountsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "accounts", "comptes et agendas")
        self.text(
            "Ajoute ici tous tes agendas : chaque rendez-vous sonnera "
            f"{app.config['lead_minutes']} minutes avant son début."
        )
        self.rows = tk.Frame(self.frame)
        self.rows.pack(anchor="w", fill="x")
        add = tk.Frame(self.frame)
        add.pack(anchor="w", pady=(14, 0))
        for text, kind in (
            ("Ajouter un compte CalDAV (Infomaniak…)", "caldav"),
            ("Ajouter une adresse iCal (Google…)", "ics"),
        ):
            tk.Button(add, text=text, font=(FONT, 10), command=partial(app.edit_account, None, kind)).pack(
                side="left", padx=(0, 8)
            )
        tk.Button(self.frame, text="Fermer", font=(FONT, 10), command=self.close).pack(anchor="e", pady=(14, 0))
        self.refresh()

    def refresh(self):
        for child in self.rows.winfo_children():
            child.destroy()
        accounts = self.app.config["accounts"]
        if not accounts:
            tk.Label(self.rows, text="Aucun agenda pour l'instant.", font=(FONT, 10), fg=COLOR_ERROR).pack(anchor="w")
        for account in accounts:
            row = tk.Frame(self.rows)
            row.pack(anchor="w", fill="x", pady=4)
            labels = tk.Frame(row)
            labels.pack(side="left", padx=(0, 16))
            tk.Label(labels, text=account_label(account), font=(FONT, 10, "bold")).pack(anchor="w")
            tk.Label(labels, text=self.app.account_status(account), font=(FONT, 9)).pack(anchor="w")
            # Packed from the right edge: the first one ends up rightmost.
            actions = [
                ("Supprimer", partial(self.remove, account)),
                ("Modifier…", partial(self.app.edit_account, account, account["type"])),
            ]
            if account["type"] == "caldav":
                actions.append(("Agendas…", partial(self.app.ask_calendars, account["id"])))
            for text, command in actions:
                tk.Button(row, text=text, font=(FONT, 9), command=command).pack(side="right", padx=(6, 0))

    def remove(self, account):
        question = f"Supprimer « {account_label(account)} » ? Ses rendez-vous ne sonneront plus."
        if messagebox.askyesno("Calendar Alarm", question, parent=self):
            self.app.remove_account(account)


class AlarmWindow(tk.Toplevel):
    def __init__(self, app, event, index):
        super().__init__(app.root)
        self.app = app
        self.event = event
        self.title("Rendez-vous")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        # Closing the window must not silence the alarm for good.
        self.protocol("WM_DELETE_WINDOW", self.snooze)

        frame = tk.Frame(self, padx=28, pady=22)
        frame.pack()
        self.countdown = tk.Label(frame, font=(FONT, 13, "bold"), fg=COLOR_ERROR)
        self.countdown.pack(anchor="w")
        tk.Label(frame, text=event.summary, font=(FONT, 20, "bold"), wraplength=520, justify="left").pack(
            anchor="w", pady=(6, 6)
        )
        details = f"{event.start:%H:%M} – {event.end:%H:%M}   ·   {event.calendar}"
        tk.Label(frame, text=details, font=(FONT, 11)).pack(anchor="w")
        if event.location:
            tk.Label(frame, text=event.location, font=(FONT, 11), wraplength=520, justify="left").pack(anchor="w")
        buttons = tk.Frame(frame)
        buttons.pack(anchor="e", pady=(20, 0))
        # takefocus=0: a key typed elsewhere when the window pops up must not press a button.
        tk.Button(
            buttons, text="Arrêter", font=(FONT, 12, "bold"), width=14, takefocus=0, command=self.stop
        ).pack(side="left", padx=(0, 10))
        snooze_text = f"Rappel {app.config['snooze_minutes']} min"
        tk.Button(buttons, text=snooze_text, font=(FONT, 12), width=14, takefocus=0, command=self.snooze).pack(
            side="left"
        )

        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2 + 30 * index
        y = (self.winfo_screenheight() - self.winfo_height()) // 3 + 30 * index
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.refresh()

    def refresh(self):
        if not self.winfo_exists():
            return
        minutes = round((self.event.start - datetime.now().astimezone()).total_seconds() / 60)
        if minutes > 0:
            text = f"Rendez-vous dans {minutes} min"
        elif minutes == 0:
            text = "Rendez-vous maintenant"
        else:
            text = f"Rendez-vous commencé depuis {-minutes} min"
        self.countdown.config(text=text)
        self.after(1000, self.refresh)

    def stop(self):
        self.app.stop_alarm(self.event)

    def snooze(self):
        self.app.snooze_alarm(self.event)


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
        def item(text, action):
            return pystray.MenuItem(text, lambda: self.post(action))

        return pystray.Menu(
            pystray.MenuItem(lambda _: self.status_text(), None, enabled=False),
            pystray.MenuItem(lambda _: self.next_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Synchroniser maintenant", self.fetcher_wake),
            item("Tester l'alarme", self.test_alarm),
            item("Comptes et agendas…", self.show_accounts),
            pystray.MenuItem(
                "Lancer au démarrage de Windows",
                lambda: self.post(self.toggle_autostart),
                checked=lambda _: autostart_command() is not None,
            ),
            item("Ouvrir le journal", lambda: os.startfile(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            item("Quitter", self.quit),
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

    def toggle_autostart(self):
        set_autostart(autostart_command() is None)
        self.tray.update_menu()

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
        self.config["accounts"].append(account)
        log.info("account added: %s", account_label(account))

    def save_caldav_account(self, account, url, username, password):
        if account is None:
            self.add_account({"type": "caldav", "url": url, "username": username, "calendars": None}, password)
        else:
            if (url, username) != (account["url"], account["username"]):
                # Another account: its calendars have to be chosen again.
                account["calendars"] = None
                self.calendar_names.pop(account["id"], None)
            account.update(url=url, username=username)
            set_secret(account, password)
        self.save_config()

    def save_ics_account(self, account, name, url):
        if account is None:
            self.add_account({"type": "ics", "name": name}, url)
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
        for per_account in (self.account_events, self.last_sync, self.sync_errors, self.calendar_names, self.owner_names):
            per_account.pop(account["id"], None)
        log.info("account removed: %s", account_label(account))
        self.merge_events()
        self.save_config()

    def open(self, name, factory):
        if name not in self.dialogs:
            self.dialogs[name] = factory()

    def show_accounts(self):
        self.open("accounts", lambda: AccountsDialog(self))

    def refresh_accounts(self):
        if "accounts" in self.dialogs:
            self.dialogs["accounts"].refresh()
        self.refresh_tray()

    def edit_account(self, account, kind, message=None):
        if kind == "caldav":
            message = message or (
                "Identifiant de synchronisation et mot de passe d'application du compte. Infomaniak : les "
                "deux se trouvent sur config.infomaniak.com (« Mes Contacts & Agendas » → « Synchronisation "
                "manuelle »)."
            )
            self.open("caldav", lambda: CaldavDialog(self, account, message))
        else:
            message = message or (
                "Colle l'adresse secrète de l'agenda. Google Agenda : Paramètres → ton agenda → "
                "« Intégrer l'agenda » → « Adresse secrète au format iCal ». Elle donne accès en "
                "lecture à l'agenda : ne la partage pas."
            )
            self.open("ics", lambda: IcsDialog(self, account, message))

    def ask_calendars(self, account_id):
        account = self.account(account_id)
        if account is not None and self.calendar_names.get(account_id):
            self.open("calendars", lambda: CalendarsDialog(self, account))

    def account_problem(self, account):
        """Why the alarms of this account cannot be trusted right now, or None."""
        account_id = account["id"]
        if account_id in self.sync_errors:
            return f"En échec : {self.sync_errors[account_id]}"[:90]
        if account["type"] == "caldav" and account["calendars"] is None:
            return "Aucun agenda choisi"
        if account_id not in self.last_sync:
            return "En attente de synchronisation"
        return None

    def account_status(self, account):
        return self.account_problem(account) or f"Synchronisé à {self.last_sync[account['id']]:%H:%M:%S}"

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
            log.info("%s: sync ok, %d events in the next 24 h", account_label(account), len(events))
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
        self.sync_errors[account_id] = "refusé par le serveur"
        self.refresh_accounts()
        what = "l'adresse iCal enregistrée" if account["type"] == "ics" else "le mot de passe enregistré"
        self.edit_account(
            account,
            account["type"],
            f"{account_label(account)} : le serveur refuse {what} (révoqué ou expiré). "
            "Ses alarmes sont suspendues tant qu'il n'est pas remplacé.",
        )

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
        for event in due_events(self.events.values(), handled, now, lead, grace):
            self.ring(event)
        for key, (ring_at, event) in list(self.snoozed.items()):
            if now >= ring_at:
                del self.snoozed[key]
                self.ring(event)

        self.refresh_health()
        self.root.after(TICK_MS, self.tick)

    def ring(self, event):
        log.info("alarm: %s at %s", event.summary, event.start.isoformat())
        self.windows[event.key] = AlarmWindow(self, event, len(self.windows))
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
        self.ring(Event(f"{TEST_PREFIX}{now.isoformat()}", start, start + timedelta(hours=1), "Test de l'alarme", "", "Test"))

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

    def stale_accounts(self):
        """Accounts without a recent successful sync."""
        now = datetime.now().astimezone()
        stale_after = max(3 * self.config["poll_seconds"], 300)
        return [
            account
            for account in self.config["accounts"]
            if account["id"] not in self.last_sync
            or (now - self.last_sync[account["id"]]).total_seconds() >= stale_after
        ]

    def is_healthy(self):
        return bool(self.config["accounts"]) and not self.stale_accounts()

    def status_text(self):
        accounts = self.config["accounts"]
        if not accounts:
            return "Non configuré"
        for account in accounts:
            problem = self.account_problem(account)
            if problem:
                return f"{account_label(account)} : {problem}"[:120]
        return f"Dernière synchro : {min(self.last_sync[account['id']] for account in accounts):%H:%M:%S}"

    def next_text(self):
        now = datetime.now().astimezone()
        upcoming = sorted((event for event in self.events.values() if event.start > now), key=lambda event: event.start)
        if not upcoming:
            return "Aucun rendez-vous dans les 24 h"
        return f"Prochain : {upcoming[0].start:%H:%M} {upcoming[0].summary}"[:80]

    def refresh_tray(self):
        self.tray.title = f"Calendar Alarm — {self.status_text()}"[:120]
        self.tray.update_menu()

    def refresh_health(self):
        healthy = self.is_healthy()
        if healthy == self.healthy:
            return
        was_healthy, self.healthy = self.healthy, healthy
        self.tray.icon = self.icons[healthy]
        self.refresh_tray()
        if was_healthy and not healthy:
            names = ", ".join(account_label(account) for account in self.stale_accounts()) or "aucun compte"
            log.warning("no successful sync recently: %s", names)
            self.tray.notify(f"Agenda non synchronisé ({names}) : les alarmes ne sont plus fiables."[:250], "Calendar Alarm")


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
            log.info("check: %s: %d events in the next 24 h", account_label(account), len(events))
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
    log.info("starting")
    app = App()
    app.root.report_callback_exception = lambda *exc_info: log.error("Tk callback failed", exc_info=exc_info)
    app.run()
    del mutex


if __name__ == "__main__":
    main()
