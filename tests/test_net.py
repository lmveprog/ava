"""the network breaker: what it cuts, and above all what it does not."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests

from ava import net as net


class FakeClock:
    """A clock we advance by hand: the window lasts 45 s, the tests don't."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NetBreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.clock = FakeClock()
        patcher = mock.patch.object(net.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(net.reset)

    def test_ouvert_par_defaut(self):
        self.assertTrue(net.reachable("voix"))
        self.assertFalse(net.is_offline())

    def test_one_failure_opens_the_circuit_for_everyone(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        # this really is shared state: the weather already pays for the voice.
        self.assertFalse(net.reachable("meteo"))
        self.assertFalse(net.reachable("agenda"))

    def test_the_circuit_closes_again_after_the_window(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        self.clock.advance(net.OFFLINE_WINDOW_S - 1)
        self.assertFalse(net.reachable("voix"))
        self.clock.advance(2)
        self.assertTrue(net.reachable("voix"))

    def test_one_success_closes_it_immediately(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        net.note_success("voix")
        self.assertTrue(net.reachable("voix"))

    def test_an_http_error_is_not_an_outage(self):
        """401, 429, 500: the server answered, so we are online.

        Confusing the two would take ava off the network for a minute on every
        expired key or blown quota — exactly when retrying has to be possible.
        """
        for status in (401, 429, 500):
            with self.subTest(status=status):
                net.reset()
                response = requests.Response()
                response.status_code = status
                exc = requests.HTTPError(response=response)
                self.assertFalse(net.note_failure("api", exc))
                self.assertTrue(net.reachable("api"))

    def test_les_pannes_de_transport_ferment(self):
        import socket
        for exc in (requests.ConnectionError("refus"),
                    requests.ConnectTimeout("trop long"),
                    requests.Timeout("silence"),
                    socket.gaierror("dns muet"),
                    TimeoutError("silence")):
            with self.subTest(exc=type(exc).__name__):
                net.reset()
                self.assertTrue(net.looks_like_outage(exc))
                net.note_failure("api", exc)
                self.assertFalse(net.reachable("api"))

    def test_a_disk_error_is_not_a_network_outage(self):
        """`OSError` nu couvre aussi « disque plein » : trop large pour couper."""
        self.assertFalse(net.looks_like_outage(OSError("écriture impossible")))
        net.note_failure("cache", OSError("écriture impossible"))
        self.assertTrue(net.reachable("cache"))

    def test_une_cause_enchainee_compte(self):
        outer = RuntimeError("echec de la synthese")
        outer.__cause__ = requests.ConnectionError("pas de route")
        self.assertTrue(net.looks_like_outage(outer))

    def test_timeout_separe_connexion_et_lecture(self):
        connect, read = net.timeout(20)
        self.assertEqual(connect, net.CONNECT_TIMEOUT_S)
        self.assertEqual(read, 20)
        self.assertLess(connect, read)

    def test_a_disabled_breaker_blocks_nothing(self):
        net.set_enabled(False)
        self.addCleanup(net.set_enabled, True)
        net.note_failure("voix", requests.ConnectionError("boum"))
        self.assertTrue(net.reachable("voix"))

    def test_status_says_how_long_is_left(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        state = net.status()
        self.assertFalse(state["online"])
        self.assertEqual(state["last_failure"], "voix")
        self.assertGreater(state["seconds_left"], 0)


class AttemptTest(unittest.TestCase):
    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.addCleanup(net.reset)

    def test_the_block_knows_it_is_pointless(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        with net.attempt("voix") as online:
            self.assertFalse(online)

    def test_the_exception_is_not_swallowed(self):
        with self.assertRaises(requests.ConnectionError):
            with net.attempt("voix") as online:
                self.assertTrue(online)
                raise requests.ConnectionError("boum")
        self.assertFalse(net.reachable("voix"))


class WiringTest(unittest.TestCase):
    """Do the modules that leave the mac really go through the guard?"""

    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.addCleanup(net.reset)

    def test_the_voice_stops_calling_the_network_once_offline(self):
        from ava.audio import voice_tts as voice_tts
        net.note_failure("voix", requests.ConnectionError("boum"))
        with mock.patch.object(voice_tts, "_mistral_chunk") as chunk:
            self.assertIsNone(voice_tts._mistral_audio("une phrase jamais dite"))
        chunk.assert_not_called()

    def test_offline_the_first_sentence_does_not_make_you_wait(self):
        """The local fallback costs 8.9 s of loading plus 9 s of synthesis.

        Eighteen seconds of silence after "ouvre spotify" is worse than no
        fallback at all: the system voice answers right away while the model
        comes up behind it.
        """
        from ava.audio import voice_tts as voice_tts
        net.note_failure("voix", requests.ConnectionError("boum"))
        with mock.patch.object(voice_tts, "_mistral_audio", return_value=None), \
                mock.patch.object(voice_tts, "local_voice_ready", return_value=False), \
                mock.patch.object(voice_tts, "_eleven_audio", return_value=None), \
                mock.patch.object(voice_tts, "warm_local_voice") as warm, \
                mock.patch.object(voice_tts, "_chatterbox_audio") as local, \
                mock.patch.object(voice_tts, "engine_name", return_value="mistral"):
            self.assertIsNone(voice_tts.synthesize("J'ouvre Spotify."))
        local.assert_not_called()          # on n'attend pas le modele froid
        warm.assert_called_once()          # mais on le monte pour la suite

    def test_once_warm_the_local_model_takes_over(self):
        from ava.audio import voice_tts as voice_tts
        from pathlib import Path as P
        net.note_failure("voix", requests.ConnectionError("boum"))
        with mock.patch.object(voice_tts, "_mistral_audio", return_value=None), \
                mock.patch.object(voice_tts, "local_voice_ready", return_value=True), \
                mock.patch.object(voice_tts, "_chatterbox_audio",
                                  return_value=P("/tmp/voix.wav")) as local, \
                mock.patch.object(voice_tts, "engine_name", return_value="mistral"):
            self.assertEqual(voice_tts.synthesize("Deuxième phrase."), P("/tmp/voix.wav"))
        local.assert_called_once()

    def test_the_news_returns_immediately(self):
        from ava.services import ai_news as ai_news
        net.note_failure("actu", requests.ConnectionError("boum"))
        with mock.patch.object(ai_news, "_feed_items") as feed:
            self.assertEqual(ai_news.fetch_items(), [])
        feed.assert_not_called()

    def test_the_web_search_returns_no_sources(self):
        from ava.services import web_research as web_research
        net.note_failure("recherche", requests.ConnectionError("boum"))
        engine = web_research.WebResearch()
        with mock.patch.object(engine.session, "get") as get:
            self.assertEqual(engine.search("météo à Marseille"), ())
        get.assert_not_called()

    def test_the_calendar_says_so_instead_of_waiting(self):
        from ava.services import google_calendar as google_calendar
        net.note_failure("agenda", requests.ConnectionError("boum"))
        calendar = google_calendar.GoogleCalendar()
        with mock.patch.object(google_calendar.requests, "request") as request:
            with self.assertRaises(google_calendar.GoogleCalendarError):
                calendar._call("GET", "/calendars/primary/events")
        request.assert_not_called()

    def test_the_local_engine_stays_reachable_offline(self):
        """LM Studio and Ollama run on the loopback.

        Putting them behind the breaker would cut ava off from her *local*
        engine because the router died — the exact opposite of the point.
        """
        from ava.brain import conversation as conversation
        net.note_failure("voix", requests.ConnectionError("boum"))
        engine = conversation.LocalConversationEngine()
        reply = {"choices": [{"message": {"content": "bonjour"}}]}
        with mock.patch.object(conversation.requests, "post") as post, \
                mock.patch.object(engine, "_discover_model", return_value="local"):
            post.return_value = mock.Mock(status_code=200,
                                          json=mock.Mock(return_value=reply),
                                          raise_for_status=mock.Mock())
            answer = engine._ask_openai_compatible([{"role": "user", "content": "salut"}], 60)
        self.assertTrue(answer.available)


class UnderstandingCacheTest(unittest.TestCase):
    """The intent cache has to survive an outage — that is its whole reason."""

    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.addCleanup(net.reset)

    def test_a_learned_phrasing_works_offline(self):
        from ava.brain import understanding as understanding
        router = understanding.IntentRouter(cache_path=Path("/nonexistent/intents.json"))
        appris = understanding.Understanding("musique_suivant", confidence=0.95)
        router._cache["balance la suite"] = appris
        net.note_failure("voix", requests.ConnectionError("boum"))
        self.assertFalse(router.available())
        with mock.patch.object(router, "_classify") as classify:
            result = router.understand("Balance la suite")
        classify.assert_not_called()
        self.assertEqual(result.intent, "musique_suivant")


if __name__ == "__main__":
    unittest.main()
