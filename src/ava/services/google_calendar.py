"""ava's google calendar: read AND write, over the rest v3 api.

we talk to the api directly with requests rather than pulling in google's sdks —
three endpoints are enough (list, insert, delete) and it keeps the venv light.
authentication comes from google_auth.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import re

import requests

from ava.services.google_auth import AUTH
from ava import net as net

API = "https://www.googleapis.com/calendar/v3"

MONTHS_FR = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
}


@dataclass(frozen=True)
class GoogleEvent:
    id: str
    title: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool = False
    location: str = ""
    calendar: str = "primary"
    link: str = ""


class GoogleCalendarError(RuntimeError):
    pass


class NotConnected(GoogleCalendarError):
    pass


def _parse(value: dict) -> tuple[dt.datetime, bool]:
    """A google "start"/"end": either dateTime, or date for an all-day event."""
    if value.get("dateTime"):
        moment = dt.datetime.fromisoformat(value["dateTime"])
        # everything comes back as naive local time, like the rest of ava.
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
        return moment, False
    day = dt.date.fromisoformat(value["date"])
    return dt.datetime.combine(day, dt.time.min), True


class GoogleCalendar:
    def __init__(self, auth=AUTH, timeout: float = 15.0) -> None:
        self.auth = auth
        self.timeout = timeout

    def connected(self) -> bool:
        return bool(self.auth.status().get("connected"))

    def _headers(self) -> dict:
        token = self.auth.access_token()
        if not token:
            raise NotConnected("Ava n'est pas connectée à Google.")
        return {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, **kwargs) -> dict:
        if not net.reachable("agenda"):
            raise GoogleCalendarError("Google est injoignable pour le moment.")
        try:
            response = requests.request(
                method, f"{API}{path}", headers=self._headers(),
                timeout=net.timeout(self.timeout), **kwargs)
        except requests.RequestException as exc:
            net.note_failure("agenda", exc)
            raise
        net.note_success("agenda")
        if response.status_code == 401:
            raise NotConnected("La connexion Google a expiré, reconnecte-toi.")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            raise GoogleCalendarError(detail or f"Google a répondu {response.status_code}.")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # --- reading ------------------------------------------------------------

    def events_for_day(self, day_offset: int = 0, *, calendar_id: str = "primary") -> tuple[GoogleEvent, ...]:
        start_day = dt.date.today() + dt.timedelta(days=int(day_offset))
        start = dt.datetime.combine(start_day, dt.time.min).astimezone()
        end = start + dt.timedelta(days=1)
        payload = self._call("GET", f"/calendars/{calendar_id}/events", params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",     # deplie les recurrences
            "orderBy": "startTime",
            "maxResults": 50,
        })
        events = []
        for item in payload.get("items", []):
            if item.get("status") == "cancelled":
                continue
            try:
                begin, all_day = _parse(item["start"])
                finish, _ = _parse(item["end"])
            except (KeyError, ValueError):
                continue
            events.append(GoogleEvent(
                id=str(item.get("id", "")),
                title=str(item.get("summary") or "Sans titre")[:200],
                start=begin, end=finish, all_day=all_day,
                location=str(item.get("location") or "")[:160],
                calendar=calendar_id,
                link=str(item.get("htmlLink") or ""),
            ))
        return tuple(events)

    # --- writing ------------------------------------------------------------

    def create_event(self, title: str, start: dt.datetime, *, minutes: int = 60,
                     location: str = "", description: str = "",
                     calendar_id: str = "primary") -> GoogleEvent:
        end = start + dt.timedelta(minutes=max(5, int(minutes)))
        body = {
            "summary": str(title or "Sans titre")[:200],
            "start": {"dateTime": start.astimezone().isoformat()},
            "end": {"dateTime": end.astimezone().isoformat()},
        }
        if location:
            body["location"] = location[:160]
        if description:
            body["description"] = description[:2000]
        item = self._call("POST", f"/calendars/{calendar_id}/events", json=body)
        begin, all_day = _parse(item["start"])
        finish, _ = _parse(item["end"])
        return GoogleEvent(
            id=str(item.get("id", "")), title=str(item.get("summary", title)),
            start=begin, end=finish, all_day=all_day,
            location=str(item.get("location") or ""), calendar=calendar_id,
            link=str(item.get("htmlLink") or ""),
        )

    def delete_event(self, event_id: str, *, calendar_id: str = "primary") -> None:
        self._call("DELETE", f"/calendars/{calendar_id}/events/{event_id}")


# --- making sense of "demain a 14h30" ---------------------------------------

def parse_french_datetime(text: str, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """Pull a date and time out of a spoken sentence.

    Deliberately simple: the shapes people actually say ("demain à 14h", "lundi
    à 9h30", "le 12 septembre à 15h"). With no time found we return nothing —
    better to ask again than to book something at midnight.
    """
    value = " " + str(text or "").lower().strip() + " "
    now = now or dt.datetime.now()

    hour = minute = None
    match = re.search(r"\b(\d{1,2})\s*(?:h|heures?)\s*(\d{1,2})?\b", value)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
    else:
        match = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", value)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
    if hour is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    if hour < 8 and re.search(r"\b(?:apres[- ]midi|après[- ]midi|soir)\b", value):
        hour += 12          # "5h de l'après-midi"

    day = now.date()
    weekdays = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    match = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS_FR) + r")\b", value)
    if match:
        number, month_name = int(match.group(1)), match.group(2)
        month = MONTHS_FR[month_name]
        year = now.year + (1 if (month, number) < (now.month, now.day) else 0)
        try:
            day = dt.date(year, month, number)
        except ValueError:
            return None
    elif "apres demain" in value or "après-demain" in value or "apres-demain" in value:
        day = now.date() + dt.timedelta(days=2)
    elif "demain" in value:
        day = now.date() + dt.timedelta(days=1)
    elif "aujourd" in value or "ce soir" in value or "tout a l heure" in value:
        day = now.date()
    else:
        for index, name in enumerate(weekdays):
            if re.search(rf"\b{name}\b", value):
                ahead = (index - now.weekday()) % 7
                # "lundi" said on a monday means next monday.
                day = now.date() + dt.timedelta(days=ahead or 7)
                break

    moment = dt.datetime.combine(day, dt.time(hour, minute))
    if moment < now - dt.timedelta(minutes=1) and not match:
        # a time already gone by with no explicit date means tomorrow.
        moment += dt.timedelta(days=1)
    return moment


def parse_duration_minutes(text: str, default: int = 60) -> int:
    value = str(text or "").lower()
    match = re.search(r"\bpendant\s+(\d{1,3})\s*(?:min|minutes?)\b", value)
    if match:
        return max(5, min(600, int(match.group(1))))
    match = re.search(r"\bpendant\s+(\d{1,2})\s*(?:h|heures?)\b", value)
    if match:
        return max(5, min(600, int(match.group(1)) * 60))
    return default


CALENDAR = GoogleCalendar()
