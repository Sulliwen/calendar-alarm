import re
import string
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

import icalendar

import calendar_alarm
import texts
from calendar_alarm import Event, due_events, extract_events

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:timed
SUMMARY:Point projet
LOCATION:Salle 2
DTSTART:20261013T120000Z
DTEND:20261013T130000Z
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Férié
DTSTART;VALUE=DATE:20261013
DTEND;VALUE=DATE:20261014
END:VEVENT
BEGIN:VEVENT
UID:cancelled
SUMMARY:Annulé
STATUS:CANCELLED
DTSTART:20261013T140000Z
DTEND:20261013T150000Z
END:VEVENT
BEGIN:VEVENT
UID:declined
SUMMARY:Refusé
DTSTART:20261013T160000Z
DTEND:20261013T170000Z
ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com
ATTENDEE;PARTSTAT=DECLINED:mailto:Me@Example.com
END:VEVENT
BEGIN:VEVENT
UID:accepted
SUMMARY:Accepté
DTSTART:20261013T180000Z
DURATION:PT30M
ATTENDEE;PARTSTAT=DECLINED:mailto:other@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly
SUMMARY:Hebdo
DTSTART:20260901T090000Z
DTEND:20260901T093000Z
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""

CALDAV = {"id": "a", "type": "caldav", "url": "https://example.com", "username": "AB12345", "calendars": ["url/mine"]}
FEED = {"id": "g", "type": "ics", "name": "Google"}
PROVIDER = {provider["key"]: provider for provider in texts.PROVIDERS}


def event(key, start):
    return Event(key, start, start + timedelta(hours=1), key, "", "cal")


def components(ics=ICS):
    return icalendar.Calendar.from_ical(ics).walk("VEVENT")


class ExtractEventsTest(unittest.TestCase):
    def setUp(self):
        self.events = {e.key.split("|")[0]: e for e in extract_events(components(), "Perso", {"me@example.com"})}

    def test_keeps_only_timed_live_undeclined_events(self):
        self.assertEqual(set(self.events), {"timed", "accepted", "weekly"})

    def test_reads_fields_and_converts_to_local_time(self):
        timed = self.events["timed"]
        self.assertEqual((timed.summary, timed.location, timed.calendar), ("Point projet", "Salle 2", "Perso"))
        self.assertEqual(timed.start, datetime.fromisoformat("2026-10-13T12:00:00+00:00"))
        self.assertEqual(timed.start.utcoffset(), datetime.now().astimezone().utcoffset())
        self.assertEqual(self.events["accepted"].end - self.events["accepted"].start, timedelta(minutes=30))

    def test_key_changes_when_the_event_moves(self):
        moved = extract_events(components(ICS.replace("20261013T120000Z", "20261013T121500Z")), "Perso", set())
        self.assertNotEqual(moved[0].key, self.events["timed"].key)


class DueEventsTest(unittest.TestCase):
    now = datetime.fromisoformat("2026-10-13T12:00:00+00:00")
    lead = timedelta(minutes=10)
    grace = timedelta(minutes=5)

    def due(self, minutes_from_now, handled=()):
        events = [event("e", self.now + timedelta(minutes=minutes_from_now))]
        return bool(due_events(events, set(handled), self.now, self.lead, self.grace))

    def test_window(self):
        self.assertFalse(self.due(11))
        self.assertTrue(self.due(10))
        # Added at the last minute, or found after a wake-up.
        self.assertTrue(self.due(2))
        self.assertTrue(self.due(-4))
        self.assertFalse(self.due(-5))

    def test_handled_events_do_not_ring_again(self):
        self.assertFalse(self.due(5, handled=["e"]))


class IcsUrlTest(unittest.TestCase):
    def test_only_https_feeds_are_accepted(self):
        normalize = calendar_alarm.normalize_ics_url
        self.assertEqual(normalize(" https://x.test/basic.ics "), "https://x.test/basic.ics")
        self.assertEqual(normalize("webcal://x.test/basic.ics"), "https://x.test/basic.ics")
        for refused in ("file:///C:/secret.txt", "http://x.test/basic.ics", "basic.ics", ""):
            self.assertIsNone(normalize(refused))


class TextsTest(unittest.TestCase):
    def test_both_languages_say_the_same_things(self):
        french, english = texts.STRINGS["fr"], texts.STRINGS["en"]
        self.assertEqual(set(french), set(english))
        for key in french:
            fields = [{name for _, name, _, _ in string.Formatter().parse(text[key]) if name} for text in (french, english)]
            self.assertEqual(*fields, key)

    def test_every_provider_has_a_name_and_instructions(self):
        for provider in texts.PROVIDERS:
            for language in texts.STRINGS.values():
                self.assertTrue(language[f"provider_{provider['key']}"])
                self.assertTrue(language[f"help_{provider['key']}"])

    def test_french_addresses_the_user_formally(self):
        informal = re.compile(r"\b(tu|ton|ta|tes|toi)\b", re.IGNORECASE)
        self.assertEqual([key for key, text in texts.STRINGS["fr"].items() if informal.search(text)], [])

    def test_no_user_text_is_left_in_the_program(self):
        source = Path(calendar_alarm.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(re.findall(r"[éèêàçùôî«»…]", code.replace('show="•"', "")), [])


class ConfigTest(unittest.TestCase):
    def test_single_account_configuration_is_migrated(self):
        legacy = {"username": "AB12345", "calendars": ["url/mine"], "lead_minutes": 15}
        with (
            mock.patch("calendar_alarm.load_json", return_value=legacy),
            mock.patch("calendar_alarm.save_json") as save_json,
            mock.patch("calendar_alarm.keyring") as keyring,
        ):
            keyring.get_password.return_value = "secret"
            config = calendar_alarm.load_config()
        (account,) = config["accounts"]
        self.assertEqual(
            {**account, "id": "x"},
            {"id": "x", "type": "caldav", "url": "https://sync.infomaniak.com", "username": "AB12345", "calendars": ["url/mine"]},
        )
        self.assertEqual(config["lead_minutes"], 15)
        keyring.set_password.assert_called_once_with(f"calendar-alarm:{account['id']}", "secret", "secret")
        keyring.delete_password.assert_called_once_with("calendar-alarm", "AB12345")
        self.assertNotIn("username", save_json.call_args.args[1])

    def test_default_accounts_are_not_shared_between_configurations(self):
        with mock.patch("calendar_alarm.load_json", return_value={}):
            calendar_alarm.load_config()["accounts"].append("x")
        self.assertEqual(calendar_alarm.DEFAULT_CONFIG["accounts"], [])


class AppTest(unittest.TestCase):
    """Drives the real windows, with the sound, the tray and the vault mocked out."""

    def setUp(self):
        for target in ("winsound.PlaySound", "keyring", "save_json"):
            patcher = mock.patch(f"calendar_alarm.{target}")
            setattr(self, target.split(".")[-1], patcher.start())
            self.addCleanup(patcher.stop)
        self.keyring.get_password.return_value = "secret"
        with mock.patch("calendar_alarm.load_json", return_value={}):
            self.app = calendar_alarm.App()
        texts.set_language("fr")
        self.app.tray = mock.Mock()
        self.app.fetcher = mock.Mock()
        self.caldav, self.feed = dict(CALDAV), dict(FEED)
        self.app.config["accounts"] = [self.caldav]
        self.app.root.after = mock.Mock()
        self.addCleanup(self.app.root.destroy)
        self.now = datetime.now().astimezone()

    def tick(self):
        self.app.tick()
        self.app.root.update()

    # --- alarms

    def test_stop_silences_the_event_for_good(self):
        soon = event("soon", self.now + timedelta(minutes=4))
        self.app.on_events("a", [soon, event("later", self.now + timedelta(hours=3))])
        self.tick()
        self.assertEqual(list(self.app.windows), ["soon"])
        self.assertTrue(self.app.sound_playing)
        self.tick()
        self.assertEqual(len(self.app.windows), 1)

        self.app.windows["soon"].stop()
        self.tick()
        self.assertFalse(self.app.windows)
        self.assertFalse(self.app.sound_playing)
        self.assertIn("soon", self.app.acked)

    def test_snooze_rings_again_even_after_the_grace_period(self):
        started = event("started", self.now - timedelta(minutes=3))
        self.app.on_events("a", [started])
        self.tick()
        self.app.windows["started"].snooze()
        self.tick()
        self.assertFalse(self.app.windows)
        self.assertFalse(self.app.sound_playing)

        self.app.snoozed["started"] = (self.now - timedelta(seconds=1), started)
        self.app.on_events("a", [])
        self.tick()
        self.assertEqual(list(self.app.windows), ["started"])
        self.assertNotIn("started", self.app.acked)

    def test_cancelled_event_stops_ringing(self):
        self.app.on_events("a", [event("soon", self.now + timedelta(minutes=4))])
        self.tick()
        self.app.on_events("a", [])
        self.assertFalse(self.app.windows)
        self.assertFalse(self.app.sound_playing)

    def test_meeting_present_in_two_accounts_rings_once_and_survives_one_of_them(self):
        self.app.config["accounts"].append(self.feed)
        soon = event("soon", self.now + timedelta(minutes=4))
        self.app.on_events("a", [soon])
        self.app.on_events("g", [soon])
        self.tick()
        self.assertEqual(list(self.app.windows), ["soon"])
        # Removed from one calendar only: still a meeting.
        self.app.on_events("g", [])
        self.assertEqual(list(self.app.windows), ["soon"])

    # --- accounts

    def fetch_caldav(self, found):
        calendars = {}
        for url in ("url/mine", "url/colleague"):
            calendars[url] = (mock.Mock(**{"search.return_value": found}), url)
        discovery = calendar_alarm.Discovery(calendars, set(), "Me")
        fetcher = calendar_alarm.Fetcher(self.app)
        with mock.patch("calendar_alarm.discover", return_value=discovery):
            return fetcher.fetch(self.caldav, "secret"), calendars

    def test_only_the_chosen_calendars_are_read(self):
        found = [mock.Mock(icalendar_instance=icalendar.Calendar.from_ical(ICS))]
        events, calendars = self.fetch_caldav(found)
        self.assertEqual({e.calendar for e in events}, {"url/mine"})
        calendars["url/colleague"][0].search.assert_not_called()

    def test_calendars_must_be_chosen_first_and_still_exist(self):
        for chosen in (None, ["url/deleted"]):
            self.caldav["calendars"] = chosen
            with self.assertRaises(calendar_alarm.CalendarsNeeded):
                self.fetch_caldav([])

    def test_ical_feed_expands_recurrences_and_spots_declined_invitations(self):
        fetcher = calendar_alarm.Fetcher(self.app)
        start = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        url = "https://calendar.test/ical/me%40example.com/private-abc/basic.ics"
        with mock.patch("calendar_alarm.download_ics", return_value=ICS.encode()) as download:
            events = fetcher.fetch_ics(self.feed, url, start, start + timedelta(days=7))
            fetcher.fetch_ics(self.feed, url, start, start + timedelta(days=7))
        self.assertEqual(download.call_count, 2)
        by_uid = {}
        for e in events:
            by_uid.setdefault(e.key.split("|")[0], []).append(e)
        self.assertEqual(set(by_uid), {"timed", "accepted", "weekly"})
        self.assertEqual(
            [e.start for e in by_uid["weekly"]], [datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)]
        )
        self.assertEqual(by_uid["weekly"][0].calendar, "Google")

    def test_first_choice_proposes_the_owner_calendar(self):
        self.caldav["calendars"] = None
        self.app.on_discovery("a", {"url/mine": "Me", "url/colleague": "Someone"}, "Me")
        self.app.ask_calendars("a")
        self.app.root.update()
        dialog = self.app.dialogs["calendars"]
        self.assertEqual({url: var.get() for url, var in dialog.choices.items()}, {"url/mine": True, "url/colleague": False})
        dialog.choices["url/mine"].set(False)
        dialog.submit()
        self.assertIn("calendars", self.app.dialogs)
        dialog.choices["url/colleague"].set(True)
        dialog.submit()
        self.assertEqual(self.caldav["calendars"], ["url/colleague"])
        self.assertNotIn("calendars", self.app.dialogs)
        self.app.fetcher.reset.assert_called()

    def test_refused_secret_reopens_the_matching_dialog(self):
        self.app.config["accounts"].append(self.feed)
        self.app.on_refused("g")
        self.app.root.update()
        self.assertIsInstance(self.app.dialogs["ics"], calendar_alarm.IcsDialog)
        self.app.dialogs["ics"].close()
        self.app.on_refused("a")
        self.app.root.update()
        self.assertIsInstance(self.app.dialogs["caldav"], calendar_alarm.CaldavDialog)
        self.assertNotIn("ics", self.app.dialogs)

    def test_adding_and_removing_an_ical_feed(self):
        self.app.show_accounts()
        self.app.edit_account(None, PROVIDER["google"])
        self.app.root.update()
        dialog = self.app.dialogs["ics"]
        self.assertEqual(dialog.label.get(), "Google Agenda")
        dialog.url.insert(0, "file:///C:/secret.txt")
        dialog.submit()
        self.assertEqual(len(self.app.config["accounts"]), 1)

        dialog.tested(None, "Google", "https://calendar.test/basic.ics")
        added = self.app.config["accounts"][1]
        self.assertEqual((added["type"], added["provider"], added["name"]), ("ics", "google", "Google"))
        self.keyring.set_password.assert_called_once_with(
            f"calendar-alarm:{added['id']}", "secret", "https://calendar.test/basic.ics"
        )
        # The address is a secret: it must stay out of the configuration file.
        self.assertNotIn("calendar.test", str(self.save_json.call_args.args[1]))

        self.app.on_events(added["id"], [event("soon", self.now + timedelta(hours=1))])
        self.app.remove_account(added)
        self.assertEqual(self.app.config["accounts"], [self.caldav])
        self.assertFalse(self.app.events)
        self.keyring.delete_password.assert_called_once_with(f"calendar-alarm:{added['id']}", "secret")

    def test_first_launch_opens_the_accounts_window(self):
        self.app.config["accounts"] = []
        self.app.root.mainloop = mock.Mock()
        with mock.patch("calendar_alarm.autostart_command", return_value=calendar_alarm.launch_command()):
            self.app.run()
        self.assertIsInstance(self.app.dialogs["accounts"], calendar_alarm.AccountsDialog)

    def test_dropdown_offers_every_provider_and_prefills_its_server(self):
        self.app.show_accounts()
        accounts = self.app.dialogs["accounts"]
        self.assertEqual(len(accounts.provider.cget("values")), len(texts.PROVIDERS))
        accounts.provider.current(texts.PROVIDERS.index(PROVIDER["icloud"]))
        accounts.add()
        self.app.root.update()
        dialog = self.app.dialogs["caldav"]
        self.assertEqual(dialog.url.get(), "https://caldav.icloud.com")
        dialog.tested(None, "https://caldav.icloud.com", "me@icloud.com", "app-password")
        self.assertEqual(self.app.config["accounts"][1]["provider"], "icloud")

    def test_changing_language_translates_menus_and_windows(self):
        self.app.show_accounts()
        self.assertEqual(self.app.next_text(), "Aucun rendez-vous dans les 24 h")
        self.app.set_language("en")
        self.addCleanup(texts.set_language, "fr")
        self.assertEqual(self.app.next_text(), "No appointment in the next 24 h")
        self.assertEqual(self.app.config["language"], "en")
        self.assertFalse(self.app.dialogs)
        self.app.show_accounts()
        self.app.root.update()
        self.assertIn("accounts and calendars", self.app.dialogs["accounts"].title())

    # --- health

    def test_health_turns_red_as_soon_as_one_account_goes_stale(self):
        self.app.config["accounts"].append(self.feed)
        self.app.on_events("a", [])
        self.tick()
        self.assertFalse(self.app.healthy)
        self.app.on_events("g", [])
        self.tick()
        self.assertTrue(self.app.healthy)
        self.assertTrue(self.app.status_text().startswith("Dernière synchro"))

        self.app.last_sync["g"] = self.now - timedelta(minutes=10)
        self.tick()
        self.assertFalse(self.app.healthy)
        self.app.tray.notify.assert_called_once()
        self.assertIn("Google", self.app.tray.notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
