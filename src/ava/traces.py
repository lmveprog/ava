"""le journal de bord d'ava : ou part le temps, et quand elle sort du mac.

openjarvis pose l'energie, la latence et le cout en contraintes de premier
ordre, au meme titre que la justesse. l'idee vaut ici : la seule question qu'on
se pose vraiment avec un assistant vocal, c'est « pourquoi elle met trois
secondes a repondre ». sans mesure, on optimise au pif.

chaque interaction laisse une ligne dans `.cache/traces.jsonl` : quelle route a
ete prise, combien de temps, et si ca a demande le reseau.

⚠️ **on n'ecrit jamais ce qui a ete dit.** ni la commande, ni la reponse — juste
la route, la duree et un drapeau reseau. un journal de tout ce qu'on dit a son
assistante vocale n'a aucune raison de trainer en clair sur le disque.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import json
from pathlib import Path
import threading
import time

from ava import paths

TRACE_PATH = paths.cache_dir("traces.jsonl")

# au-dela, on repart d'un fichier neuf : une ligne fait ~90 octets, donc 20 000
# lignes tiennent largement l'historique utile sans jamais peser.
MAX_LINES = 20000

# la promesse de l'entete ne tient que si elle est **appliquee** : `record`
# acceptait n'importe quelle chaine, donc un appelant distrait pouvait y glisser
# la commande de l'utilisateur. seuls ces champs passent, les autres sont
# silencieusement jetes.
ALLOWED_FIELDS = frozenset({"route", "network", "engine", "ok", "count", "cached"})

_lock = threading.Lock()
_enabled = True


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def record(event: str, seconds: float = 0.0, **fields) -> None:
    """Ajoute une ligne. Ne leve jamais : une mesure ratee n'est pas un incident."""
    if not _enabled:
        return
    line = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": str(event)[:40],
        "ms": round(max(0.0, float(seconds)) * 1000),
    }
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            line[key] = value[:60] if isinstance(value, str) else value
    try:
        with _lock:
            TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with TRACE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


@contextmanager
def span(event: str, **fields):
    """Mesure un bloc. Les champs peuvent etre completes pendant l'execution.

        with traces.span("commande") as trace:
            ...
            trace["route"] = "competence"
    """
    started = time.monotonic()
    extra: dict = dict(fields)
    try:
        yield extra
    finally:
        record(event, time.monotonic() - started, **extra)


def _read(limit: int = MAX_LINES) -> list[dict]:
    try:
        lines = TRACE_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for raw in lines[-limit:]:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def prune() -> None:
    """Coupe le journal quand il depasse la taille retenue."""
    entries = _read()
    if len(entries) < MAX_LINES:
        return
    try:
        with _lock:
            TRACE_PATH.write_text(
                "".join(json.dumps(entry, ensure_ascii=False) + "\n"
                        for entry in entries[-MAX_LINES // 2:]),
                encoding="utf-8")
    except OSError:
        pass


@dataclass(frozen=True)
class Stat:
    label: str
    count: int
    median_ms: int
    p90_ms: int
    network_share: float


def _percentile(values: list[int], share: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def summary(event: str = "commande", entries: list[dict] | None = None) -> list[Stat]:
    """Repartition par route : combien, quelle latence, combien de reseau."""
    rows = [entry for entry in (_read() if entries is None else entries)
            if entry.get("event") == event]
    routes: dict[str, list[dict]] = {}
    for row in rows:
        routes.setdefault(str(row.get("route", "inconnue")), []).append(row)
    stats = []
    for label, items in routes.items():
        durations = [int(item.get("ms", 0)) for item in items]
        network = sum(1 for item in items if item.get("network"))
        stats.append(Stat(
            label=label,
            count=len(items),
            median_ms=_percentile(durations, 0.5),
            p90_ms=_percentile(durations, 0.9),
            network_share=network / len(items) if items else 0.0,
        ))
    stats.sort(key=lambda stat: stat.count, reverse=True)
    return stats


def report(event: str = "commande") -> str:
    """Le tableau lisible, pour `doctor.py` et la ligne de commande."""
    stats = summary(event)
    if not stats:
        return "Aucune trace pour le moment."
    width = max(len(stat.label) for stat in stats)
    lines = [f"{'route'.ljust(width)}   n   médiane      p90   réseau"]
    for stat in stats:
        lines.append(
            f"{stat.label.ljust(width)} {stat.count:3}  {stat.median_ms:6} ms "
            f"{stat.p90_ms:6} ms   {stat.network_share * 100:3.0f} %")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
    print()
    print(report("voix"))
