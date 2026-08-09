"""what ava does when the router is dead.

the offline fallbacks were wired in everywhere, but nobody had ever timed them
for real. measured: wifi up, internet down — a captive portal or a router that
gave up, far more common than an unplugged card — and **every** call waits out
its full timeout before returning:

    voice (mistral)     20 s   per sentence
    weather             10 s   (+ 10 s of geocoding)
    news                10 s   per feed, six feeds
    google calendar     15 s   (+ 20 s if the token needs refreshing)

so an offline "bonjour ava" meant minutes of silence before the first word, and
the bill came due again on every sentence of the briefing. the fallbacks worked
fine — they just arrived far too late to be any use.

hence this module: **one place knows whether we're online**. the first call to
break its teeth opens the circuit for everyone; the ones after return straight
away and go directly to their local fallback. after `OFFLINE_WINDOW_S` one call
is let through — if it lands, the circuit closes again. no periodic probe:
ordinary traffic is enough to decide.

⚠️ **an http error is not a network outage.** a 401, a 429, a 500 prove the
opposite — we reached somebody. treating those as an outage would cut ava off
the network for a minute over an expired key. only transport failures
(connection refused, silent dns, timeout) open the circuit, which is the whole
job of `looks_like_outage`.
"""

from __future__ import annotations

from contextlib import contextmanager
import socket
import threading
import time

from ava import traces as traces

# connecting to a host that's there takes tens of milliseconds; past three
# seconds it isn't going to connect at all. reading, on the other hand, is
# allowed to take a while (speech has to be generated). that's the distinction
# a bare `timeout=20` can't make — we used to wait 20 s only to learn there was
# nobody on the other end.
CONNECT_TIMEOUT_S = 3.0

# long enough not to re-pay the timeout on every sentence of a briefing, short
# enough that the wifi coming back shows up without restarting ava.
OFFLINE_WINDOW_S = 45.0

_lock = threading.Lock()
_blocked_until = 0.0
_last_failure = ""
_enabled = True


def set_enabled(value: bool) -> None:
    """Turn the breaker itself off (tests, or hand diagnosis)."""
    global _enabled
    _enabled = bool(value)
    if not _enabled:
        reset()


def reset() -> None:
    """Close the circuit again. Called when a call comes back fine."""
    global _blocked_until, _last_failure
    with _lock:
        _blocked_until = 0.0
        _last_failure = ""


def timeout(read_s: float = 20.0) -> tuple[float, float]:
    """The (connect, read) pair to hand to requests.

    `requests` takes a tuple; the first number bounds the tcp handshake, the
    second the wait for the response. That's what takes outage detection from
    20 s down to 3 s without shortening the slow calls that are legitimate.
    """
    return (CONNECT_TIMEOUT_S, max(1.0, float(read_s)))


def looks_like_outage(exc: BaseException) -> bool:
    """Is the network missing, or is the server saying no?

    An `HTTPError` (401, 429, 500…) proves we reached somebody: that's not an
    outage, and ruling it out here keeps an expired key from taking ava off the
    network. Only transport failures count.
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
    """Do we already know we're offline? Touches no network."""
    if not _enabled:
        return False
    with _lock:
        return time.monotonic() < _blocked_until


def reachable(where: str = "") -> bool:
    """Is it worth trying? Call this before any round trip.

    `False` while the window runs, `True` after: the circuit goes half-open on
    its own, and the next real call is what decides.
    """
    if not is_offline():
        return True
    traces.record("reseau", route=str(where or "inconnu"), ok=False, network=False)
    return False


def note_failure(where: str, exc: BaseException | None = None) -> bool:
    """Report a failed call. True if the circuit just opened."""
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
    """A round trip landed: the network is there, whatever we thought."""
    if is_offline():
        print(f"[réseau] de retour ({where})")
        traces.record("reseau", route=str(where or "inconnu"), ok=True, network=True)
    reset()


@contextmanager
def attempt(where: str):
    """Wrap a network call: guard on the way in, verdict on the way out.

        with net.attempt("voix") as online:
            if not online:
                return None
            ...

    Success declares itself if the block doesn't raise, and a transport failure
    opens the circuit without being swallowed — the caller keeps its own
    fallback.
    """
    online = reachable(where)
    try:
        yield online
    except BaseException as exc:  # noqa: BLE001 — we reclassify, we don't swallow
        if online:
            note_failure(where, exc)
        raise
    else:
        if online:
            note_success(where)


def status() -> dict:
    """For `ava-doctor` and the settings panel."""
    with _lock:
        remaining = max(0.0, _blocked_until - time.monotonic())
        return {
            "online": remaining <= 0,
            "seconds_left": round(remaining, 1),
            "last_failure": _last_failure,
        }
