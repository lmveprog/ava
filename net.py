"""ce qu'ava fait quand la box est morte.

le repli hors-ligne etait cable partout, mais personne ne l'avait jamais
chronometre en conditions reelles. mesure faite : wifi present, internet
absent — le cas du captif ou de la box en rade, bien plus frequent qu'une carte
reseau coupee —, **chaque** appel attend son timeout complet avant de rendre la
main :

    voix (mistral)      20 s   par phrase
    meteo               10 s   (+ 10 s de geocodage)
    actu                10 s   par flux, six flux
    agenda google       15 s   (+ 20 s si le jeton doit se rafraichir)

un « bonjour ava » hors ligne, c'etait donc plusieurs minutes de silence avant
le premier mot, et le prix se repayait a chaque phrase du briefing. le repli
fonctionnait — il arrivait juste beaucoup trop tard pour servir a quelque chose.

d'ou ce module : **un seul endroit sait si on est en ligne**. le premier appel
qui se casse les dents ferme le circuit pour tout le monde ; les suivants
rendent la main tout de suite et partent directement sur leur repli local. au
bout de `OFFLINE_WINDOW_S`, on laisse repasser un appel — s'il aboutit, le
circuit se rouvre. pas de sonde periodique : le trafic normal suffit a decider.

⚠️ **une reponse http d'erreur n'est pas une panne de reseau.** un 401, un 429,
un 500 prouvent au contraire qu'on est en ligne : les traiter comme une coupure
couperait ava du reseau pendant une minute sur une simple cle expiree. seules
les pannes de transport (connexion refusee, dns muet, timeout) ferment le
circuit — c'est tout l'objet de `looks_like_outage`.
"""

from __future__ import annotations

from contextlib import contextmanager
import socket
import threading
import time

import traces

# se connecter a un hote joignable prend quelques dizaines de millisecondes ;
# au-dela de trois secondes, ca ne se connectera pas. la lecture, elle, a le
# droit d'etre longue (une synthese vocale se fabrique). c'est la distinction
# que `timeout=20` seul ne permet pas de faire : on attendait 20 s pour
# apprendre qu'il n'y avait personne au bout du fil.
CONNECT_TIMEOUT_S = 3.0

# assez long pour ne pas repayer le timeout a chaque phrase d'un briefing,
# assez court pour qu'un retour du wifi se voie sans redemarrer ava.
OFFLINE_WINDOW_S = 45.0

_lock = threading.Lock()
_blocked_until = 0.0
_last_failure = ""
_enabled = True


def set_enabled(value: bool) -> None:
    """Coupe le coupe-circuit lui-meme (tests, ou diagnostic a la main)."""
    global _enabled
    _enabled = bool(value)
    if not _enabled:
        reset()


def reset() -> None:
    """Rouvre le circuit. Appele au retour d'un appel qui aboutit."""
    global _blocked_until, _last_failure
    with _lock:
        _blocked_until = 0.0
        _last_failure = ""


def timeout(read_s: float = 20.0) -> tuple[float, float]:
    """Le couple (connexion, lecture) a passer a requests.

    `requests` accepte un tuple ; le premier nombre borne la poignee de main
    tcp, le second l'attente de la reponse. c'est ce qui fait passer la
    detection d'une coupure de 20 s a 3 s, sans raccourcir les appels lents
    qui, eux, sont legitimes.
    """
    return (CONNECT_TIMEOUT_S, max(1.0, float(read_s)))


def looks_like_outage(exc: BaseException) -> bool:
    """Est-ce le reseau qui manque, ou le serveur qui repond non ?

    Un `HTTPError` (401, 429, 500...) prouve qu'on a joint quelqu'un : ce n'est
    pas une coupure, et l'ecarter ici evite de couper ava du reseau pour une
    cle expiree. On ne retient que les pannes de transport.
    """
    if isinstance(exc, (socket.gaierror, socket.timeout, ConnectionError, TimeoutError)):
        return True
    name = type(exc).__name__
    if name in ("HTTPError", "TooManyRedirects"):
        return False
    if name in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
                "ConnectTimeoutError", "NewConnectionError", "NameResolutionError",
                "MaxRetryError", "SSLError", "ProxyError"):
        return True
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return bool(cause) and looks_like_outage(cause)


def is_offline() -> bool:
    """Sait-on deja qu'on est hors ligne ? Ne touche pas au reseau."""
    if not _enabled:
        return False
    with _lock:
        return time.monotonic() < _blocked_until


def reachable(where: str = "") -> bool:
    """Ca vaut-il le coup d'essayer ? A appeler avant tout aller-retour.

    Rend `False` tant que la fenetre court, `True` ensuite : le circuit repasse
    de lui-meme en demi-ouvert, et c'est le prochain vrai appel qui tranche.
    """
    if not is_offline():
        return True
    traces.record("reseau", route=str(where or "inconnu"), ok=False, network=False)
    return False


def note_failure(where: str, exc: BaseException | None = None) -> bool:
    """Signale un appel rate. Rend True si le circuit vient de se fermer."""
    if not _enabled:
        return False
    if exc is not None and not looks_like_outage(exc):
        return False
    global _blocked_until, _last_failure
    with _lock:
        already = time.monotonic() < _blocked_until
        _blocked_until = time.monotonic() + OFFLINE_WINDOW_S
        _last_failure = str(where or "inconnu")
    if not already:
        print(f"[réseau] injoignable ({where}) — replis locaux pendant "
              f"{int(OFFLINE_WINDOW_S)} s")
        traces.record("reseau", route=str(where or "inconnu"), ok=False, network=True)
    return not already


def note_success(where: str = "") -> None:
    """Un aller-retour a abouti : le reseau est la, quoi qu'on ait cru."""
    if is_offline():
        print(f"[réseau] de retour ({where})")
        traces.record("reseau", route=str(where or "inconnu"), ok=True, network=True)
    reset()


@contextmanager
def attempt(where: str):
    """Enveloppe un appel reseau : garde a l'entree, verdict a la sortie.

        with net.attempt("voix") as online:
            if not online:
                return None
            ...

    Le succes se declare tout seul si le bloc ne leve pas, et une panne de
    transport ferme le circuit sans etre avalee : l'appelant garde son propre
    repli.
    """
    online = reachable(where)
    try:
        yield online
    except BaseException as exc:  # noqa: BLE001 — on requalifie, on n'avale pas
        if online:
            note_failure(where, exc)
        raise
    else:
        if online:
            note_success(where)


def status() -> dict:
    """Pour `doctor.py` et le panneau de reglages."""
    with _lock:
        remaining = max(0.0, _blocked_until - time.monotonic())
        return {
            "online": remaining <= 0,
            "seconds_left": round(remaining, 1),
            "last_failure": _last_failure,
        }
