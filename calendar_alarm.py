"""Calendar alarm: rings before every event of an Infomaniak (CalDAV) calendar.

Runs in the background with a tray icon. Every event gets an alarm, whether
or not it carries a reminder and whoever created it.
"""

import ctypes
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
import winsound
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import caldav
import keyring
import pystray
from caldav.lib.error import AuthorizationError
from PIL import Image, ImageDraw

APP = "calendar-alarm"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "calendar-alarm.log"

DEFAULT_CONFIG = {
    "url": "https://sync.infomaniak.com",
    "username": "",
    "lead_minutes": 10,
    "snooze_minutes": 5,
    # An event found after it started still rings during this many minutes.
    "late_grace_minutes": 5,
    "poll_seconds": 60,
    "sound": r"C:\Windows\Media\Alarm01.wav",
    # URLs of the calendars to watch; None until the user has chosen them.
    "calendars": None,
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

log = logging.getLogger(APP)


@dataclass(frozen=True)
class Event:
    key: str
    start: datetime
    end: datetime
    summary: str
    location: str
    calendar: str


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


def is_declined(component, my_addresses):
    attendees = component.get("ATTENDEE") or []
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        address = str(attendee).lower().removeprefix("mailto:")
        if address in my_addresses and str(attendee.params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def extract_events(ical, calendar_name, my_addresses):
    """Timed, non-cancelled, non-declined events of an (already expanded) icalendar object."""
    events = []
    for component in ical.walk("VEVENT"):
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


def my_addresses(principal, config):
    addresses = {email.lower() for email in config["my_emails"]}
    if "@" in config["username"]:
        addresses.add(config["username"].lower())
    try:
        for address in principal.calendar_user_address_set():
            addresses.add(str(address).lower().removeprefix("mailto:"))
    except Exception as exc:
        log.info("no calendar-user-address-set: %r", exc)
    return addresses


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


class CalendarsNeeded(Exception):
    """The calendars to watch are not chosen yet, or one of them no longer exists."""


@dataclass
class Account:
    # url -> (caldav calendar, display name), for every calendar holding events
    calendars: dict
    addresses: set
    owner_name: str


def discover(url, username, password, config):
    """Raises AuthorizationError on refused credentials."""
    client = caldav.DAVClient(url=url, username=username, password=password, timeout=30)
    principal = client.principal()
    calendars = {}
    for calendar in principal.calendars():
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
    try:
        owner_name = principal.get_display_name() or ""
    except Exception:
        owner_name = ""
    return Account(calendars, my_addresses(principal, config), owner_name)


class Fetcher(threading.Thread):
    """Polls the calendars and hands the upcoming events to the UI thread."""

    def __init__(self, app):
        super().__init__(daemon=True, name="fetcher")
        self.app = app
        self.wake = threading.Event()
        # Set after refused credentials, until new ones are saved.
        self.blocked = False
        self.account = None
        self.discovered_at = 0.0

    def reset(self):
        self.account = None
        self.blocked = False
        self.wake.set()

    def run(self):
        while True:
            delay = self.cycle()
            self.wake.wait(delay)
            self.wake.clear()

    def cycle(self):
        app = self.app
        credentials = app.credentials()
        if credentials is None or self.blocked:
            return None
        try:
            events = self.fetch(*credentials)
        except AuthorizationError:
            log.warning("credentials refused by the server")
            self.blocked = True
            self.account = None
            app.post(app.on_auth_failed)
            return None
        except CalendarsNeeded:
            self.blocked = True
            app.post(app.ask_calendars)
            return None
        except Exception as exc:
            log.warning("sync failed: %r", exc)
            self.account = None
            message = str(exc) or type(exc).__name__
            app.post(lambda: app.on_sync_error(message))
            return RETRY_SECONDS
        app.post(lambda: app.on_events(events))
        return app.config["poll_seconds"]

    def fetch(self, url, username, password):
        config = self.app.config
        if self.account is None or time.monotonic() - self.discovered_at > REDISCOVER_SECONDS:
            account = self.account = discover(url, username, password, config)
            self.discovered_at = time.monotonic()
            names = {url: name for url, (_, name) in account.calendars.items()}
            self.app.post(lambda: self.app.on_account(names, account.owner_name))
        selected = config["calendars"]
        if selected is None or not set(selected) <= self.account.calendars.keys():
            raise CalendarsNeeded
        now = datetime.now().astimezone()
        start = now - timedelta(minutes=config["late_grace_minutes"])
        events = []
        for calendar, name in (self.account.calendars[url] for url in selected):
            for found in calendar.search(start=start, end=now + HORIZON, event=True, expand=True):
                events.extend(extract_events(found.icalendar_instance, name, self.account.addresses))
        return events


class CredentialsDialog(tk.Toplevel):
    def __init__(self, app, message):
        super().__init__(app.root)
        self.app = app
        self.title("Calendar Alarm — identifiants Infomaniak")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.close)

        frame = tk.Frame(self, padx=20, pady=16)
        frame.pack()
        tk.Label(frame, text=message, font=(FONT, 10), wraplength=420, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        self.url = self.field(frame, 1, "Serveur", app.config["url"])
        self.username = self.field(frame, 2, "Identifiant (ex. AB12345)", app.config["username"])
        self.password = self.field(frame, 3, "Mot de passe d'application", "", show="•")
        self.status = tk.Label(frame, text="", font=(FONT, 9), fg=COLOR_ERROR, wraplength=420, justify="left")
        self.status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = tk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.submit_button = tk.Button(buttons, text="Tester et enregistrer", font=(FONT, 10), command=self.submit)
        self.submit_button.pack(side="left", padx=(0, 8))
        tk.Button(buttons, text="Annuler", font=(FONT, 10), command=self.close).pack(side="left")
        self.bind("<Return>", lambda _: self.submit())
        (self.password if app.config["username"] else self.username).focus_set()

    def field(self, frame, row, label, value, show=""):
        tk.Label(frame, text=label, font=(FONT, 10)).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
        entry = tk.Entry(frame, font=(FONT, 10), width=36, show=show)
        entry.insert(0, value)
        entry.grid(row=row, column=1, pady=3)
        return entry

    def submit(self):
        url, username, password = self.url.get().strip(), self.username.get().strip(), self.password.get()
        if not (url and username and password):
            self.status.config(text="Les trois champs sont obligatoires.")
            return
        self.submit_button.config(state="disabled")
        self.status.config(text="Test de la connexion…", fg="black")
        threading.Thread(target=self.check, args=(url, username, password), daemon=True).start()

    def check(self, url, username, password):
        try:
            account = discover(url, username, password, self.app.config)
            error = None if account.calendars else "Connexion réussie, mais aucun agenda trouvé."
        except AuthorizationError:
            error = "Identifiant ou mot de passe refusé."
        except Exception as exc:
            error = f"Connexion impossible : {exc or type(exc).__name__}"
        self.app.post(lambda: self.checked(url, username, password, error))

    def checked(self, url, username, password, error):
        if not self.winfo_exists():
            return
        if error:
            self.status.config(text=error, fg=COLOR_ERROR)
            self.submit_button.config(state="normal")
            return
        self.app.save_credentials(url, username, password)
        self.close()

    def close(self):
        self.app.credentials_dialog = None
        self.destroy()


class CalendarsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Calendar Alarm — agendas surveillés")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.close)

        frame = tk.Frame(self, padx=20, pady=16)
        frame.pack()
        tk.Label(
            frame,
            text="Coche les agendas dont les rendez-vous doivent sonner.",
            font=(FONT, 10),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))
        if app.config["calendars"] is None:
            # First choice: propose the calendar named after the account owner.
            selected = {url for url, name in app.calendar_names.items() if name == app.owner_name}
        else:
            selected = set(app.config["calendars"])
        self.choices = {}
        for url, name in sorted(app.calendar_names.items(), key=lambda item: item[1].lower()):
            self.choices[url] = tk.BooleanVar(self, value=url in selected)
            tk.Checkbutton(frame, text=name, variable=self.choices[url], font=(FONT, 10)).pack(anchor="w")
        self.status = tk.Label(frame, text="", font=(FONT, 9), fg=COLOR_ERROR)
        self.status.pack(anchor="w", pady=(8, 0))
        buttons = tk.Frame(frame)
        buttons.pack(anchor="e", pady=(8, 0))
        tk.Button(buttons, text="Enregistrer", font=(FONT, 10), command=self.submit).pack(side="left", padx=(0, 8))
        tk.Button(buttons, text="Annuler", font=(FONT, 10), command=self.close).pack(side="left")

    def submit(self):
        urls = [url for url, checked in self.choices.items() if checked.get()]
        if not urls:
            self.status.config(text="Coche au moins un agenda.")
            return
        self.app.save_calendars(urls)
        self.close()

    def close(self):
        self.app.calendars_dialog = None
        self.destroy()


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


class App:
    def __init__(self):
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
        # key -> ISO start, for the alarms stopped by the user.
        self.acked = load_json(STATE_PATH, {}).get("acked", {})
        self.events = {}
        # key -> (ring again at, event)
        self.snoozed = {}
        self.windows = {}
        self.inbox = queue.SimpleQueue()
        self.last_sync = None
        self.sync_error = None
        self.healthy = None
        self.sound_playing = False
        self.credentials_dialog = None
        self.calendars_dialog = None
        # url -> display name of every calendar of the account
        self.calendar_names = {}
        self.owner_name = ""
        self.last_tick = time.time()

        self.root = tk.Tk()
        self.root.withdraw()
        self.icons = {True: make_icon(COLOR_OK), False: make_icon(COLOR_ERROR)}
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
            item("Agendas surveillés…", self.ask_calendars),
            item("Identifiants Infomaniak…", lambda: self.ask_credentials("Saisis les identifiants de ton agenda.")),
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
        if self.credentials() is None:
            self.ask_credentials(
                "Premier lancement : saisis l'identifiant de synchronisation Infomaniak "
                "et un mot de passe d'application. Il sera enregistré dans le "
                "Gestionnaire d'identifications Windows."
            )
        self.root.after(TICK_MS, self.tick)
        self.root.mainloop()

    def quit(self):
        self.set_sound(False)
        self.tray.stop()
        self.root.destroy()

    def fetcher_wake(self):
        self.fetcher.wake.set()

    # --- credentials ----------------------------------------------------

    def credentials(self):
        username = self.config["username"]
        if not username:
            return None
        password = keyring.get_password(APP, username)
        if not password:
            return None
        return self.config["url"], username, password

    def save_config(self):
        stored = {key: value for key, value in self.config.items() if value != DEFAULT_CONFIG.get(key)}
        save_json(CONFIG_PATH, stored)
        self.fetcher.reset()

    def save_credentials(self, url, username, password):
        keyring.set_password(APP, username, password)
        if (url, username) != (self.config["url"], self.config["username"]):
            # Another account: its calendars have to be chosen again.
            self.config["calendars"] = None
            self.calendar_names = {}
        self.config.update(url=url, username=username)
        log.info("credentials saved for %s", username)
        self.save_config()

    def ask_credentials(self, message):
        if self.credentials_dialog is None:
            self.credentials_dialog = CredentialsDialog(self, message)

    def save_calendars(self, urls):
        self.config["calendars"] = urls
        log.info("watching: %s", [self.calendar_names.get(url, url) for url in urls])
        self.save_config()

    def ask_calendars(self):
        if self.calendars_dialog is None and self.calendar_names:
            self.calendars_dialog = CalendarsDialog(self)

    def toggle_autostart(self):
        set_autostart(autostart_command() is None)
        self.tray.update_menu()

    # --- fetcher callbacks ----------------------------------------------

    def on_account(self, calendar_names, owner_name):
        self.calendar_names = calendar_names
        self.owner_name = owner_name

    def on_events(self, events):
        now = datetime.now().astimezone()
        if self.last_sync is None or self.sync_error:
            log.info("sync ok: %d events in the next 24 h", len(events))
        self.events = {event.key: event for event in events}
        self.last_sync = now
        self.sync_error = None

        # An upcoming event that left the calendar was cancelled or moved: forget its alarm.
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
        self.refresh_tray()

    def on_sync_error(self, message):
        self.sync_error = message
        self.refresh_tray()

    def on_auth_failed(self):
        self.sync_error = "identifiants refusés"
        self.refresh_tray()
        self.ask_credentials(
            "Infomaniak refuse le mot de passe enregistré (révoqué ou expiré). "
            "Les alarmes sont suspendues tant qu'il n'est pas remplacé."
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

    def is_healthy(self):
        if self.last_sync is None:
            return False
        stale_after = max(3 * self.config["poll_seconds"], 300)
        return (datetime.now().astimezone() - self.last_sync).total_seconds() < stale_after

    def status_text(self):
        if self.credentials() is None:
            return "Non configuré"
        if self.config["calendars"] is None:
            return "Aucun agenda choisi"
        if self.last_sync is None:
            return f"Pas encore synchronisé ({self.sync_error})" if self.sync_error else "Synchronisation…"
        text = f"Dernière synchro : {self.last_sync:%H:%M:%S}"
        return f"{text} — en échec depuis ({self.sync_error})"[:120] if self.sync_error else text

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
            log.warning("no successful sync recently: %s", self.sync_error)
            self.tray.notify("L'agenda n'est plus synchronisé : les alarmes ne sont plus fiables.", "Calendar Alarm")


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


def main():
    setup_logging()
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
