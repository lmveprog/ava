"""le coupe-circuit reseau : ce qu'il coupe, et surtout ce qu'il ne coupe pas."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests

from ava import net as net


class FakeClock:
    """Un temps qu'on avance a la main : la fenetre dure 45 s, pas les tests."""

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

    def test_une_panne_ferme_le_circuit_pour_tout_le_monde(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        # c'est bien un etat partage : la meteo paie deja pour la voix.
        self.assertFalse(net.reachable("meteo"))
        self.assertFalse(net.reachable("agenda"))

    def test_le_circuit_se_rouvre_apres_la_fenetre(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        self.clock.advance(net.OFFLINE_WINDOW_S - 1)
        self.assertFalse(net.reachable("voix"))
        self.clock.advance(2)
        self.assertTrue(net.reachable("voix"))

    def test_un_succes_rouvre_immediatement(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        net.note_success("voix")
        self.assertTrue(net.reachable("voix"))

    def test_une_erreur_http_n_est_pas_une_coupure(self):
        """401, 429, 500 : le serveur repond, donc on est en ligne.

        Les confondre couperait ava du reseau pendant une minute a chaque cle
        expiree ou quota depasse — exactement quand il faut pouvoir reessayer.
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

    def test_une_panne_disque_n_est_pas_une_panne_reseau(self):
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

    def test_desactive_le_coupe_circuit_ne_bloque_rien(self):
        net.set_enabled(False)
        self.addCleanup(net.set_enabled, True)
        net.note_failure("voix", requests.ConnectionError("boum"))
        self.assertTrue(net.reachable("voix"))

    def test_status_dit_ce_qui_reste(self):
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

    def test_le_bloc_sait_qu_il_ne_sert_a_rien(self):
        net.note_failure("voix", requests.ConnectionError("boum"))
        with net.attempt("voix") as online:
            self.assertFalse(online)

    def test_l_exception_n_est_pas_avalee(self):
        with self.assertRaises(requests.ConnectionError):
            with net.attempt("voix") as online:
                self.assertTrue(online)
                raise requests.ConnectionError("boum")
        self.assertFalse(net.reachable("voix"))


class WiringTest(unittest.TestCase):
    """Les modules qui sortent du mac passent-ils vraiment par la garde ?"""

    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.addCleanup(net.reset)

    def test_la_voix_ne_rappelle_pas_le_reseau_une_fois_hors_ligne(self):
        from ava.audio import voice_tts as voice_tts
        net.note_failure("voix", requests.ConnectionError("boum"))
        with mock.patch.object(voice_tts, "_mistral_chunk") as chunk:
            self.assertIsNone(voice_tts._mistral_audio("une phrase jamais dite"))
        chunk.assert_not_called()

    def test_hors_ligne_la_premiere_phrase_ne_fait_pas_attendre(self):
        """Le repli local coute 8,9 s de chargement + 9 s de synthese.

        Dix-huit secondes de silence apres « ouvre spotify », c'est pire que
        pas de repli du tout : la voix systeme repond tout de suite pendant que
        le modele monte derriere.
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

    def test_une_fois_chaud_le_modele_local_reprend_la_main(self):
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

    def test_l_actu_rend_la_main_tout_de_suite(self):
        from ava.services import ai_news as ai_news
        net.note_failure("actu", requests.ConnectionError("boum"))
        with mock.patch.object(ai_news, "_feed_items") as feed:
            self.assertEqual(ai_news.fetch_items(), [])
        feed.assert_not_called()

    def test_la_recherche_web_rend_zero_source(self):
        from ava.services import web_research as web_research
        net.note_failure("recherche", requests.ConnectionError("boum"))
        engine = web_research.WebResearch()
        with mock.patch.object(engine.session, "get") as get:
            self.assertEqual(engine.search("météo à Marseille"), ())
        get.assert_not_called()

    def test_l_agenda_le_dit_au_lieu_d_attendre(self):
        from ava.services import google_calendar as google_calendar
        net.note_failure("agenda", requests.ConnectionError("boum"))
        calendar = google_calendar.GoogleCalendar()
        with mock.patch.object(google_calendar.requests, "request") as request:
            with self.assertRaises(google_calendar.GoogleCalendarError):
                calendar._call("GET", "/calendars/primary/events")
        request.assert_not_called()

    def test_le_moteur_local_reste_joignable_hors_ligne(self):
        """LM Studio et Ollama tournent sur la boucle locale.

        Les mettre derriere le coupe-circuit reviendrait a couper ava de son
        moteur *local* parce que la box a saute — exactement l'inverse du but.
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
    """Le cache d'intentions doit survivre a la coupure : c'est sa raison d'etre."""

    def setUp(self) -> None:
        net.set_enabled(True)
        net.reset()
        self.addCleanup(net.reset)

    def test_une_tournure_apprise_marche_hors_ligne(self):
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
