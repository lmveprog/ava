import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google_calendar as gc  # noqa: E402


class FrenchDateTests(unittest.TestCase):
    # un jeudi, pour que « lundi » et « samedi » tombent des deux cotes.
    NOW = dt.datetime(2026, 8, 6, 10, 0)

    def parse(self, text):
        return gc.parse_french_datetime(text, now=self.NOW)

    def test_tomorrow_with_a_round_hour(self):
        self.assertEqual(self.parse("ajoute un rdv demain à 14h"),
                         dt.datetime(2026, 8, 7, 14, 0))

    def test_minutes_are_kept(self):
        self.assertEqual(self.parse("réunion demain à 9h30"),
                         dt.datetime(2026, 8, 7, 9, 30))

    def test_explicit_date(self):
        self.assertEqual(self.parse("le 12 septembre à 15h"),
                         dt.datetime(2026, 9, 12, 15, 0))

    def test_a_weekday_means_the_next_one(self):
        # jeudi + « lundi » = le lundi suivant, pas le lundi passe.
        self.assertEqual(self.parse("lundi à 8h"), dt.datetime(2026, 8, 10, 8, 0))

    def test_a_weekday_that_is_today_means_next_week(self):
        self.assertEqual(self.parse("jeudi à 8h"), dt.datetime(2026, 8, 13, 8, 0))

    def test_an_hour_already_past_rolls_to_tomorrow(self):
        self.assertEqual(self.parse("un point à 8h"), dt.datetime(2026, 8, 7, 8, 0))

    def test_today_stays_today(self):
        self.assertEqual(self.parse("aujourd'hui à 18h"), dt.datetime(2026, 8, 6, 18, 0))

    def test_afternoon_shifts_a_small_hour(self):
        self.assertEqual(self.parse("demain à 5h de l'après-midi"),
                         dt.datetime(2026, 8, 7, 17, 0))

    def test_no_hour_means_no_guess(self):
        self.assertIsNone(self.parse("ajoute un rendez-vous demain"))

    def test_an_impossible_hour_is_refused(self):
        self.assertIsNone(self.parse("rendez-vous demain à 47h"))


class DurationTests(unittest.TestCase):
    def test_default_is_an_hour(self):
        self.assertEqual(gc.parse_duration_minutes("un point demain à 9h"), 60)

    def test_minutes(self):
        self.assertEqual(gc.parse_duration_minutes("demain à 9h pendant 30 minutes"), 30)

    def test_hours(self):
        self.assertEqual(gc.parse_duration_minutes("demain à 9h pendant 2 heures"), 120)


class ParseGoogleTimesTests(unittest.TestCase):
    def test_all_day_events(self):
        moment, all_day = gc._parse({"date": "2026-08-08"})
        self.assertTrue(all_day)
        self.assertEqual(moment, dt.datetime(2026, 8, 8, 0, 0))

    def test_timed_events_lose_their_timezone(self):
        moment, all_day = gc._parse({"dateTime": "2026-08-08T14:30:00+02:00"})
        self.assertFalse(all_day)
        self.assertIsNone(moment.tzinfo)


class ClientTests(unittest.TestCase):
    def test_reading_without_a_token_raises_not_connected(self):
        calendar = gc.GoogleCalendar()
        with patch.object(calendar.auth, "access_token", return_value=""):
            with self.assertRaises(gc.NotConnected):
                calendar.events_for_day(0)

    def test_cancelled_events_are_dropped(self):
        calendar = gc.GoogleCalendar()
        payload = {"items": [
            {"id": "1", "summary": "Annulé", "status": "cancelled",
             "start": {"dateTime": "2026-08-08T10:00:00+02:00"},
             "end": {"dateTime": "2026-08-08T11:00:00+02:00"}},
            {"id": "2", "summary": "Point produit",
             "start": {"dateTime": "2026-08-08T14:00:00+02:00"},
             "end": {"dateTime": "2026-08-08T15:00:00+02:00"}},
        ]}
        with patch.object(calendar, "_call", return_value=payload):
            events = calendar.events_for_day(0)
        self.assertEqual([e.title for e in events], ["Point produit"])

    def test_create_event_sends_a_start_and_an_end(self):
        calendar = gc.GoogleCalendar()
        answer = {"id": "9", "summary": "Dentiste",
                  "start": {"dateTime": "2026-08-09T14:00:00+02:00"},
                  "end": {"dateTime": "2026-08-09T14:45:00+02:00"}}
        with patch.object(calendar, "_call", return_value=answer) as call:
            event = calendar.create_event("Dentiste", dt.datetime(2026, 8, 9, 14, 0), minutes=45)
        body = call.call_args.kwargs["json"]
        self.assertEqual(body["summary"], "Dentiste")
        self.assertIn("dateTime", body["start"])
        self.assertIn("dateTime", body["end"])
        self.assertEqual(event.title, "Dentiste")


if __name__ == "__main__":
    unittest.main()
