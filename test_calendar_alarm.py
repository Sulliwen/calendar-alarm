import unittest
from datetime import datetime, timedelta
from unittest import mock

import icalendar

import calendar_alarm
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
END:VCALENDAR
"""


def event(key, start):
    return Event(key, start, start + timedelta(hours=1), key, "", "cal")


class ExtractEventsTest(unittest.TestCase):
    def setUp(self):
        self.events = {
            e.key.split("|")[0]: e
            for e in extract_events(icalendar.Calendar.from_ical(ICS), "Perso", {"me@example.com"})
        }

    def test_keeps_only_timed_live_undeclined_events(self):
        self.assertEqual(set(self.events), {"timed", "accepted"})

    def test_reads_fields_and_converts_to_local_time(self):
        timed = self.events["timed"]
        self.assertEqual((timed.summary, timed.location, timed.calendar), ("Point projet", "Salle 2", "Perso"))
        self.assertEqual(timed.start, datetime.fromisoformat("2026-10-13T12:00:00+00:00"))
        self.assertEqual(timed.start.utcoffset(), datetime.now().astimezone().utcoffset())
        self.assertEqual(self.events["accepted"].end - self.events["accepted"].start, timedelta(minutes=30))

    def test_key_changes_when_the_event_moves(self):
        moved = extract_events(
            icalendar.Calendar.from_ical(ICS.replace("20261013T120000Z", "20261013T121500Z")), "Perso", set()
        )
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


class AppTest(unittest.TestCase):
    """Drives the real windows, with the sound, the tray and the vault mocked out."""

    def setUp(self):
        for target in ("winsound.PlaySound", "keyring.get_password", "save_json"):
            patcher = mock.patch(f"calendar_alarm.{target}")
            setattr(self, target.split(".")[-1], patcher.start())
            self.addCleanup(patcher.stop)
        self.get_password.return_value = "secret"
        with mock.patch("calendar_alarm.load_json", return_value={}):
            self.app = calendar_alarm.App()
        self.app.tray = mock.Mock()
        self.app.fetcher = mock.Mock()
        self.app.config["username"] = "AB12345"
        self.app.config["calendars"] = ["url/mine"]
        self.app.root.after = mock.Mock()
        self.addCleanup(self.app.root.destroy)
        self.now = datetime.now().astimezone()

    def tick(self):
        self.app.tick()
        self.app.root.update()

    def test_stop_silences_the_event_for_good(self):
        soon = event("soon", self.now + timedelta(minutes=4))
        self.app.on_events([soon, event("later", self.now + timedelta(hours=3))])
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
        self.app.on_events([started])
        self.tick()
        self.app.windows["started"].snooze()
        self.tick()
        self.assertFalse(self.app.windows)
        self.assertFalse(self.app.sound_playing)

        self.app.snoozed["started"] = (self.now - timedelta(seconds=1), started)
        self.app.on_events([])
        self.tick()
        self.assertEqual(list(self.app.windows), ["started"])
        self.assertNotIn("started", self.app.acked)

    def test_cancelled_event_stops_ringing(self):
        self.app.on_events([event("soon", self.now + timedelta(minutes=4))])
        self.tick()
        self.app.on_events([])
        self.assertFalse(self.app.windows)
        self.assertFalse(self.app.sound_playing)

    def test_refused_credentials_reopen_the_dialog(self):
        self.app.on_auth_failed()
        self.app.root.update()
        self.assertIsNotNone(self.app.credentials_dialog)
        self.app.credentials_dialog.close()
        self.assertIsNone(self.app.credentials_dialog)

    def fetch(self, found):
        calendars = {}
        for url in ("url/mine", "url/colleague"):
            calendars[url] = (mock.Mock(**{"search.return_value": found}), url)
        account = calendar_alarm.Account(calendars, set(), "Me")
        fetcher = calendar_alarm.Fetcher(self.app)
        with mock.patch("calendar_alarm.discover", return_value=account):
            return fetcher.fetch("https://example.com", "AB12345", "secret"), calendars

    def test_only_the_chosen_calendars_are_read(self):
        found = [mock.Mock(icalendar_instance=icalendar.Calendar.from_ical(ICS))]
        events, calendars = self.fetch(found)
        self.assertEqual({e.calendar for e in events}, {"url/mine"})
        calendars["url/colleague"][0].search.assert_not_called()

    def test_calendars_must_be_chosen_first_and_still_exist(self):
        for chosen in (None, ["url/deleted"]):
            self.app.config["calendars"] = chosen
            with self.assertRaises(calendar_alarm.CalendarsNeeded):
                self.fetch([])

    def test_first_choice_proposes_the_owner_calendar(self):
        self.app.config["calendars"] = None
        self.app.on_account({"url/mine": "Me", "url/colleague": "Someone"}, "Me")
        self.app.ask_calendars()
        self.app.root.update()
        dialog = self.app.calendars_dialog
        self.assertEqual({url: var.get() for url, var in dialog.choices.items()}, {"url/mine": True, "url/colleague": False})
        dialog.choices["url/mine"].set(False)
        dialog.submit()
        self.assertIsNotNone(self.app.calendars_dialog)
        dialog.choices["url/colleague"].set(True)
        dialog.submit()
        self.assertEqual(self.app.config["calendars"], ["url/colleague"])
        self.assertIsNone(self.app.calendars_dialog)
        self.app.fetcher.reset.assert_called()

    def test_health_turns_red_when_sync_goes_stale(self):
        self.app.on_events([])
        self.tick()
        self.assertTrue(self.app.healthy)
        self.app.last_sync = self.now - timedelta(minutes=10)
        self.tick()
        self.assertFalse(self.app.healthy)
        self.app.tray.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
