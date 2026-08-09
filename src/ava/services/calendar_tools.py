"""Lecture seule de Calendar.app pour les briefings d'Ava."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
import subprocess
import sys


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: dt.datetime
    end: dt.datetime
    calendar: str = ""
    location: str = ""
    all_day: bool = False


@dataclass(frozen=True)
class CalendarReply:
    available: bool
    text: str
    events: tuple[CalendarEvent, ...] = ()


_JXA = r'''
const Calendar = Application("Calendar");
const offset = __DAY_OFFSET__;
const now = new Date();
const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
const output = [];

for (const calendar of Calendar.calendars()) {
  let events = [];
  try {
    events = calendar.events.whose({_and: [
      {startDate: {_lessThan: end}},
      {endDate: {_greaterThan: start}}
    ]})();
  } catch (_) {
    // Certains comptes Calendar refusent les filtres whose. Le repli reste
    // borne pour ne jamais aspirer tout un historique ancien.
    events = calendar.events().slice(0, 500);
  }
  for (const event of events) {
    try {
      const eventStart = event.startDate();
      const eventEnd = event.endDate();
      if (!(eventStart < end && eventEnd > start)) continue;
      output.push({
        title: String(event.summary() || "Sans titre"),
        start: eventStart.toISOString(),
        end: eventEnd.toISOString(),
        calendar: String(calendar.name() || ""),
        location: String(event.location() || ""),
        allDay: Boolean(event.alldayEvent())
      });
    } catch (_) {}
  }
}
output.sort((a, b) => a.start.localeCompare(b.start));
JSON.stringify(output.slice(0, 30));
'''


def _parse_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone()


def _clean(value: object, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit]


class MacCalendar:
    """Expose uniquement les rendez-vous visibles, sans création ni édition."""

    def __init__(self, timeout_s: float = 12) -> None:
        self.timeout_s = timeout_s

    def open(self) -> bool:
        if sys.platform != "darwin":
            return False
        result = subprocess.run(
            ["open", "-a", "Calendar"], capture_output=True, text=True,
            check=False, timeout=8,
        )
        return result.returncode == 0

    def events_for_day(self, day_offset: int = 0) -> tuple[CalendarEvent, ...]:
        if sys.platform != "darwin":
            raise RuntimeError("Calendar est disponible uniquement sur macOS")
        offset = max(-7, min(365, int(day_offset)))
        script = _JXA.replace("__DAY_OFFSET__", str(offset))
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, check=False, timeout=self.timeout_s,
        )
        if result.returncode != 0:
            detail = _clean(result.stderr or result.stdout, 400)
            raise RuntimeError(detail or "Calendar n'a pas autorise la lecture")
        try:
            payload = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Reponse Calendar illisible") from exc

        events: list[CalendarEvent] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                events.append(CalendarEvent(
                    title=_clean(item.get("title")) or "Sans titre",
                    start=_parse_datetime(str(item["start"])),
                    end=_parse_datetime(str(item["end"])),
                    calendar=_clean(item.get("calendar"), 80),
                    location=_clean(item.get("location"), 120),
                    all_day=bool(item.get("allDay")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(sorted(events, key=lambda event: event.start))

    def summary(self, day_offset: int = 0) -> CalendarReply:
        label = "aujourd'hui" if day_offset == 0 else "demain" if day_offset == 1 else "ce jour-là"
        try:
            events = self.events_for_day(day_offset)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            message = str(exc).lower()
            if "not authorized" in message or "autor" in message or "-1743" in message:
                text = (
                    "J'ai besoin de l'autorisation Calendar. Ouvre Réglages Système, "
                    "Confidentialité et sécurité, Automatisation, puis autorise Ava ou Python à contrôler Calendar."
                )
            else:
                text = "Je n'arrive pas à lire Calendar pour le moment. Vérifie que l'application est disponible."
            return CalendarReply(False, text)

        if not events:
            return CalendarReply(True, f"Tu n'as rien de prévu {label} dans Calendar.", events)

        details: list[str] = []
        for event in events[:6]:
            if event.all_day:
                item = f"toute la journée : {event.title}"
            else:
                item = f"à {event.start.strftime('%H:%M')} : {event.title}"
            if event.location:
                item += f", à {event.location}"
            details.append(item)
        prefix = f"Tu as {len(events)} rendez-vous {label}" if len(events) > 1 else f"Tu as un rendez-vous {label}"
        suffix = "" if len(events) <= 6 else f" Il y en a encore {len(events) - 6}."
        return CalendarReply(True, prefix + " : " + " ; ".join(details) + "." + suffix, events)

