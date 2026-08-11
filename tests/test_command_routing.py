import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ava import app as jarvis
from ava.mac.computer_use import ActionOutcome, ComputerIntent
from ava.brain.conversation import ConversationReply
from ava.services.google_calendar import GoogleEvent
from ava.mac.screen_vision import VisionReply
from ava.services.obsidian import ObsidianMemory
from ava.services.web_research import ResearchReply, Source


def _event(title: str, start: dt.datetime, minutes: int = 60) -> GoogleEvent:
    return GoogleEvent(id="x", title=title, start=start,
                       end=start + dt.timedelta(minutes=minutes))


class CommandRoutingTests(unittest.TestCase):
    def setUp(self):
        jarvis.ASSISTANT_STATE.dormant()
        jarvis.set_assistant_state(jarvis.AvaState.THINKING, "test", force=True)
        # a throwaway vault: routing tests must never touch the real memory.
        self._vault = tempfile.TemporaryDirectory()
        self._memory = patch.object(
            jarvis, "MEMORY", ObsidianMemory(Path(self._vault.name)),
        )
        self._memory.start()

    def tearDown(self):
        self._memory.stop()
        self._vault.cleanup()
        jarvis.ASSISTANT_STATE.dormant()

    def test_close_one_app_quits_it_politely(self):
        with patch.object(jarvis, "_osascript") as osa, \
                patch.object(jarvis, "resolve_app", return_value="Spotify"), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("ferme spotify")
        osa.assert_called_once_with('tell application "Spotify" to quit')
        speak.assert_called_once_with("Je ferme Spotify.")

    def test_stop_the_music_still_pauses_instead_of_quitting(self):
        with patch.object(jarvis, "_osascript") as osa, patch.object(jarvis, "speak"):
            jarvis.execute_command("arrête la musique")
        osa.assert_called_once_with('tell application "Spotify" to pause')

    def test_remember_goes_to_the_obsidian_vault(self):
        with patch.object(jarvis.MEMORY, "remember", return_value="le wifi est lent") as remember, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("retiens que le wifi est lent")
        remember.assert_called_once_with("le wifi est lent")
        self.assertIn("retenu", speak.call_args_list[0][0][0].lower())

    def test_recall_speaks_the_known_facts(self):
        with patch.object(jarvis.MEMORY, "recall", return_value=["le wifi est lent"]), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("qu'est-ce que tu sais sur le wifi")
        self.assertIn("le wifi est lent", speak.call_args_list[0][0][0])

    def test_reading_mails_uses_the_imap_summary(self):
        summary = jarvis.mailbox.MailSummary(True, unread=2, previews=(("Jean", "devis"),))
        with patch.object(jarvis.mailbox, "credentials", return_value=("a@b.c", "x")), \
                patch.object(jarvis.mailbox, "fetch_unread", return_value=summary), \
                patch.object(jarvis, "speak") as speak, \
                patch.object(jarvis, "open_url") as open_url:
            jarvis.execute_command("lis mes mails")
        open_url.assert_not_called()
        self.assertIn("2 mails non lus", speak.call_args_list[0][0][0])

    def test_opening_mails_still_opens_gmail(self):
        with patch.object(jarvis, "open_url") as open_url, patch.object(jarvis, "speak"):
            jarvis.execute_command("ouvre mes mails")
        self.assertIn("mail.google.com", open_url.call_args[0][0])

    def test_dictated_note_lands_in_the_vault(self):
        with patch.object(jarvis.MEMORY, "quick_note") as note, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("prends note d'acheter du lait")
        note.assert_called_once_with("acheter du lait")
        self.assertIn("noté", speak.call_args_list[0][0][0])

    def test_agent_mode_needs_a_goal(self):
        with patch.object(jarvis.SCREEN_AGENT, "run") as run, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("prends la main")
        run.assert_not_called()
        self.assertIn("prends la main", speak.call_args_list[0][0][0])

    def test_agent_mode_runs_with_a_goal(self):
        from ava.mac.screen_agent import AgentResult
        result = AgentResult(True, "C'est fait.", steps=2)
        with patch.object(jarvis.SCREEN_AGENT, "run", return_value=result) as run, \
                patch.object(jarvis.COMPUTER_USE.controller, "accessibility_enabled",
                             return_value=True), \
                patch.object(jarvis, "screen_bounds", return_value=(0, 0, 1440, 900)), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("prends la main et ouvre mes téléchargements")
        self.assertEqual(run.call_args[0][0], "ouvre mes telechargements")
        speak.assert_any_call("C'est fait.")

    def test_every_exchange_is_journaled_in_the_vault(self):
        with patch.object(jarvis.MEMORY, "log_interaction") as log, \
                patch.object(jarvis, "speak"):
            jarvis.execute_command("quelle heure est-il")
        log.assert_called_once()
        self.assertIn("heure", log.call_args[0][0])

    def test_keeps_spotify_next_intent(self):
        with patch.object(jarvis, "_spotify") as spotify, patch.object(jarvis, "speak"):
            jarvis.execute_command("morceau suivant")
        spotify.assert_called_once_with("next track")

    def test_morning_spotify_is_random_when_no_uri_is_configured(self):
        previous = jarvis.SPOTIFY_URI
        jarvis.SPOTIFY_URI = ""
        try:
            with patch.object(jarvis, "_osascript") as osascript:
                jarvis.play_spotify()
        finally:
            jarvis.SPOTIFY_URI = previous
        script = osascript.call_args.args[0]
        self.assertIn("set shuffling to true", script)
        self.assertIn("next track", script)

    def test_morning_spotify_keeps_an_explicit_uri(self):
        previous = jarvis.SPOTIFY_URI
        jarvis.SPOTIFY_URI = "spotify:playlist:test"
        try:
            with patch.object(jarvis, "_osascript") as osascript:
                jarvis.play_spotify()
        finally:
            jarvis.SPOTIFY_URI = previous
        self.assertIn('play track "spotify:playlist:test"', osascript.call_args.args[0])

    def test_morning_briefing_opens_on_the_moment_and_ends_by_announcing_the_apps(self):
        with patch.object(jarvis, "calendar_sentence", return_value="Rien de prévu."), \
                patch.object(jarvis, "weather_sentence", return_value="Il fait beau."), \
                patch.object(jarvis, "ai_news_sentence", return_value="Une actualite."), \
                patch.object(jarvis.promethee, "active_session", return_value=None):
            text = jarvis.build_welcome_text()
        self.assertIn(jarvis.USER_NAME, text.split(".")[0])
        self.assertIn(jarvis.spoken_date(), text)
        self.assertIn("Rien de prévu.", text)
        self.assertIn("Prométhée", text)
        self.assertTrue(text.endswith("Bon travail !"))

    def test_the_briefing_leads_with_the_agenda_not_the_weather(self):
        """The order isn't decorative: whatever commits the day goes first."""
        with patch.object(jarvis, "calendar_sentence", return_value="AGENDA."), \
                patch.object(jarvis, "weather_sentence", return_value="METEO."), \
                patch.object(jarvis, "ai_news_sentence", return_value="ACTU."), \
                patch.object(jarvis.promethee, "active_session", return_value=None):
            text = jarvis.build_welcome_text()
        self.assertLess(text.index("AGENDA."), text.index("METEO."))
        self.assertLess(text.index("METEO."), text.index("ACTU."))

    def test_the_briefing_no_longer_opens_on_filler(self):
        # "C'est Ava, j'espère que tu vas bien !" came back word for word every morning.
        with patch.object(jarvis, "calendar_sentence", return_value=""), \
                patch.object(jarvis, "weather_sentence", return_value=""), \
                patch.object(jarvis, "ai_news_sentence", return_value=""), \
                patch.object(jarvis.promethee, "active_session", return_value=None):
            text = jarvis.build_welcome_text()
        self.assertNotIn("j'espère que tu vas bien", text)

    def test_the_quote_is_attributed(self):
        with patch.object(jarvis, "calendar_sentence", return_value=""), \
                patch.object(jarvis, "weather_sentence", return_value=""), \
                patch.object(jarvis, "ai_news_sentence", return_value=""), \
                patch.object(jarvis.promethee, "active_session", return_value=None):
            text = jarvis.build_welcome_text()
        # the opener rotates with the day; what doesn't move is the author.
        self.assertIn(jarvis.quotes.of_the_day().author, text)

    def test_it_says_bonsoir_in_the_evening(self):
        self.assertEqual(jarvis.greeting_word(dt.datetime(2026, 8, 8, 9, 0)), "Bonjour")
        self.assertEqual(jarvis.greeting_word(dt.datetime(2026, 8, 8, 20, 0)), "Bonsoir")

    def test_a_missing_news_item_is_skipped_rather_than_announced(self):
        # before: "l'actualité est indisponible pour le moment", said out loud.
        with patch.object(jarvis, "ai_news_item", return_value={}):
            self.assertEqual(jarvis.ai_news_sentence(), "")

    def test_the_weather_never_says_the_same_temperature_twice(self):
        with patch.object(jarvis, "weather_info", return_value={
                "city": "Marseille", "temp": 33, "tmax": 33, "tmin": 25,
                "desc": "ciel dégagé"}):
            sentence = jarvis.weather_sentence()
        self.assertEqual(sentence.count("33"), 1)

    def test_morning_briefing_does_not_promise_a_session_already_running(self):
        with patch.object(jarvis, "calendar_sentence", return_value=""), \
                patch.object(jarvis, "weather_sentence", return_value=""), \
                patch.object(jarvis, "ai_news_sentence", return_value=""), \
                patch.object(jarvis, "daily_quote", return_value="Continue."), \
                patch.object(jarvis.promethee, "active_session",
                             return_value={"id": "x", "task": "Deep work", "started_at": 0}):
            text = jarvis.build_welcome_text()
        self.assertIn("tourne déjà", text)
        self.assertNotIn("Je te lance une session", text)

    def test_the_workspace_goes_up_while_she_talks(self):
        previous_apps = jarvis.APPS
        # (name, slot, url): an app can be opened on a specific page.
        jarvis.APPS = [("Notion", "tr", ""), ("Dia", "tl", "https://x.com")]
        events = []

        def record_ui(name, *_args):
            events.append(f"ui:{name}")

        def record_process(args, **_kwargs):
            if args and args[0] in {"say", "afplay"}:
                events.append("speech")

        try:
            with patch.object(jarvis, "build_startup_payload", return_value={"loading": False}), \
                    patch.object(jarvis, "ui", side_effect=record_ui), \
                    patch.object(jarvis, "play_spotify", side_effect=lambda: events.append("spotify")), \
                    patch.object(jarvis, "get_welcome", side_effect=lambda: ("briefing", None)), \
                    patch.object(jarvis.subprocess, "run", side_effect=record_process), \
                    patch.object(jarvis, "screen_bounds", return_value=(0, 0, 1440, 900)), \
                    patch.object(jarvis, "start_workspace", side_effect=lambda: events.append("espace")), \
                    patch.object(jarvis, "set_assistant_state"), \
                    patch.object(jarvis, "_drain_wake_queue"), \
                    patch.object(jarvis, "return_to_idle"):
                jarvis.run_welcome_flow()
        finally:
            jarvis.APPS = previous_apps

        self.assertLess(events.index("ui:startup"), events.index("speech"))
        # the workspace goes up **before** the speech, not after: the windows
        # arrange themselves during the briefing instead of following it in
        # silence.
        self.assertLess(events.index("espace"), events.index("speech"))
        # the scene only closes once the speaking is over.
        self.assertLess(events.index("speech"), events.index("ui:finish_startup"))

    def test_typed_bonjour_ava_starts_the_same_ritual(self):
        with patch.object(jarvis, "trigger_welcome") as trigger:
            result = jarvis.submit_text_command("Bonjour Ava")
        self.assertTrue(result["accepted"])
        trigger.assert_called_once_with("bonjour ava (texte)")

    def test_computer_use_is_routed_before_legacy_intents(self):
        intent = ComputerIntent("screenshot", summary="capture")
        result = ActionOutcome(True, True, "capture terminee", intent=intent)
        with patch.object(jarvis, "parse_computer_intent", return_value=intent), \
                patch.object(jarvis.COMPUTER_USE, "handle", return_value=result) as handle, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("capture d'ecran")
        handle.assert_called_once()
        speak.assert_called_once_with("capture terminee")

    def test_discussion_uses_local_engine(self):
        reply = ConversationReply(True, "Une reponse locale.", "test")
        with patch.object(jarvis.CONVERSATION, "ask", return_value=reply) as ask, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("parle moi de la robotique")
        ask.assert_called_once()
        speak.assert_called_once_with("Une reponse locale.")

    def test_background_notification_returns_to_visible_idle(self):
        jarvis.ASSISTANT_STATE.dormant()
        with patch.object(jarvis, "speak") as speak:
            jarvis.notify("Minuteur termine")
        speak.assert_called_once_with("Minuteur termine")
        self.assertEqual(jarvis.ASSISTANT_STATE.snapshot.state, jarvis.AvaState.IDLE)

    def test_calendar_request_is_routed_before_open_app(self):
        events = (_event("Point produit", dt.datetime.now().replace(hour=14, minute=30)),)
        with patch.object(jarvis.CALENDAR, "open") as open_calendar, \
                patch.object(jarvis, "agenda_events", return_value=(events, "google")) as read, \
                patch.object(jarvis, "_open_target") as open_target, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("ouvre mon agenda et dis moi ce qui est prévu aujourd'hui")
        open_calendar.assert_called_once()
        read.assert_called_once_with(0)
        open_target.assert_not_called()
        self.assertIn("Point produit", speak.call_args.args[0])

    def test_tomorrow_reads_the_next_day(self):
        with patch.object(jarvis, "agenda_events", return_value=((), "google")) as read, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("qu'est-ce que j'ai de prévu demain dans mon agenda ?")
        read.assert_called_once_with(1)
        self.assertIn("demain", speak.call_args.args[0])

    def test_adding_an_event_writes_to_google_instead_of_reading(self):
        created = _event("Dentiste", dt.datetime.now() + dt.timedelta(days=1))
        with patch.object(jarvis.GOOGLE_CALENDAR, "connected", return_value=True), \
                patch.object(jarvis.GOOGLE_CALENDAR, "create_event", return_value=created) as create, \
                patch.object(jarvis, "_calendar_summary") as summary, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("ajoute un rendez-vous dentiste demain à 14 heures")
        summary.assert_not_called()
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0], "dentiste")
        self.assertEqual(create.call_args.args[1].hour, 14)
        self.assertIn("Dentiste", speak.call_args.args[0])

    def test_adding_an_event_without_google_explains_how_to_connect(self):
        with patch.object(jarvis.GOOGLE_CALENDAR, "connected", return_value=False), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("ajoute un rendez-vous demain à 14 heures dans mon agenda")
        self.assertIn("réglages", speak.call_args.args[0])

    def test_event_titles_are_cleaned_before_being_spoken(self):
        # calendar titles are stuffed with emoji, and the voice breaks its teeth on them.
        self.assertEqual(jarvis.spoken_title("💻 Exo code #1 — chronométré"),
                         "Exo code numéro 1 — chronométré")
        self.assertEqual(jarvis.spoken_title("🍽️ Déjeuner"), "Déjeuner")
        self.assertEqual(jarvis.spoken_title("Point produit"), "Point produit")
        self.assertEqual(jarvis.spoken_title("   "), "Sans titre")
        self.assertEqual(jarvis.spoken_title("✅ Auto-test flash"), "Auto-test flash")

    def test_the_briefing_never_speaks_an_emoji(self):
        events = (_event("📚 RAG + Évaluation", dt.datetime.now() + dt.timedelta(hours=2)),)
        with patch.object(jarvis, "agenda_events", return_value=(events, "google")):
            sentence = jarvis.calendar_sentence()
        self.assertIn("RAG + Évaluation", sentence)
        self.assertNotIn("📚", sentence)

    def test_agenda_falls_back_to_calendar_app_when_google_is_down(self):
        with patch.object(jarvis.GOOGLE_CALENDAR, "connected", return_value=True), \
                patch.object(jarvis.GOOGLE_CALENDAR, "events_for_day", side_effect=RuntimeError("boom")), \
                patch.object(jarvis.CALENDAR, "events_for_day", return_value=()) as local:
            events, source = jarvis.agenda_events(0)
        local.assert_called_once_with(0)
        self.assertEqual((events, source), ((), "calendar"))

    def test_om_match_uses_official_internal_research(self):
        reply = ResearchReply(True, "OM contre Strasbourg.", (Source("OM", "https://www.om.fr/"),))
        with patch.object(jarvis.WEB_RESEARCH, "next_om_match", return_value=reply) as match, \
                patch.object(jarvis, "speak") as speak, patch.object(jarvis, "ui"):
            jarvis.execute_command("quand est le prochain match de l'OM ?")
        match.assert_called_once()
        speak.assert_called_once_with(reply.answer)

    def test_screen_question_captures_then_analyzes(self):
        reply = VisionReply(True, "Le message indique une erreur réseau.", None, "test")
        with patch.object(jarvis.SCREEN_VISION, "capture_and_analyze", return_value=reply) as vision, \
                patch.object(jarvis.time, "sleep"), patch.object(jarvis, "ui"), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("quel est ce problème ?")
        vision.assert_called_once()
        speak.assert_called_once_with(reply.text)

    def test_voice_conversation_continues_without_wake_word(self):
        previous = (jarvis.CONTINUOUS_LISTENING, jarvis.MAX_CONTINUOUS_TURNS)
        jarvis.CONTINUOUS_LISTENING = True
        jarvis.MAX_CONTINUOUS_TURNS = 4
        try:
            with patch.object(jarvis, "execute_command") as execute, \
                    patch.object(jarvis, "_record_utterance", return_value=b"audio") as record, \
                    patch.object(jarvis, "transcribe", side_effect=["ouvre Notes", ""]), \
                    patch.object(jarvis, "ui"), patch.object(jarvis.time, "sleep"):
                jarvis._run_continuous_conversation("quelle heure est-il", display_initial=True)
        finally:
            jarvis.CONTINUOUS_LISTENING, jarvis.MAX_CONTINUOUS_TURNS = previous
        self.assertEqual([call.args[0] for call in execute.call_args_list], [
            "quelle heure est-il", "ouvre Notes",
        ])
        self.assertEqual(record.call_count, 2)

    def test_saying_stop_ends_the_followup_session(self):
        previous = (jarvis.CONTINUOUS_LISTENING, jarvis.MAX_CONTINUOUS_TURNS)
        jarvis.CONTINUOUS_LISTENING = True
        jarvis.MAX_CONTINUOUS_TURNS = 4
        try:
            with patch.object(jarvis, "execute_command") as execute, \
                    patch.object(jarvis, "_record_utterance", return_value=b"audio"), \
                    patch.object(jarvis, "transcribe", return_value="stop"), \
                    patch.object(jarvis, "speak") as speak, patch.object(jarvis, "ui"), \
                    patch.object(jarvis.time, "sleep"):
                jarvis._run_continuous_conversation("ouvre Notes", display_initial=True)
        finally:
            jarvis.CONTINUOUS_LISTENING, jarvis.MAX_CONTINUOUS_TURNS = previous
        execute.assert_called_once_with("ouvre Notes")
        speak.assert_called_once_with("D'accord, je reste disponible.")


if __name__ == "__main__":
    unittest.main()


class BriefingFreshnessTests(unittest.TestCase):
    """The startup scene froze for about a minute when the briefing changed."""

    def test_the_scene_shows_exactly_what_ava_will_say(self):
        # the transcript must come from the text already synthesised, not a recompute
        # qui pourrait differer (agenda ou heure ayant bouge entre-temps).
        with patch.object(jarvis, "build_welcome_text") as rebuild, \
                patch.object(jarvis, "weather_info", return_value=None), \
                patch.object(jarvis, "ai_news_item", return_value={}):
            payload = jarvis.build_startup_payload(fetch_news=True, briefing="Texte déjà dit.")
        rebuild.assert_not_called()
        self.assertEqual(payload["briefing"], "Texte déjà dit.")

    def test_without_a_text_the_payload_builds_its_own(self):
        with patch.object(jarvis, "build_welcome_text", return_value="Frais.") as rebuild, \
                patch.object(jarvis, "weather_info", return_value=None), \
                patch.object(jarvis, "ai_news_item", return_value={}):
            payload = jarvis.build_startup_payload(fetch_news=True)
        rebuild.assert_called_once()
        self.assertEqual(payload["briefing"], "Frais.")

    def test_the_warmer_leaves_ava_alone_while_she_speaks(self):
        calls = []

        def fake_sleep(_s):
            if len(calls) >= 2:
                raise KeyboardInterrupt
            calls.append("tick")

        jarvis._flow_active.set()
        try:
            with patch.object(jarvis.time, "sleep", fake_sleep), \
                    patch.object(jarvis, "get_welcome") as warm:
                with self.assertRaises(KeyboardInterrupt):
                    jarvis.keep_welcome_warm(interval_s=0)
            warm.assert_not_called()
        finally:
            jarvis._flow_active.clear()


class RobustnessTests(unittest.TestCase):
    """No command may be able to kill the interaction quietly."""

    def test_a_crash_downstream_still_gets_an_answer(self):
        with patch.object(jarvis, "_dispatch_command", side_effect=RuntimeError("boum")), \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("n'importe quoi")
        self.assertIn("pas réussi", speak.call_args.args[0])

    def test_a_dead_network_does_not_kill_the_research_fallback(self):
        with patch.object(jarvis.WEB_RESEARCH, "answer", side_effect=RuntimeError("réseau coupé")), \
                patch.object(jarvis, "set_assistant_state"), \
                patch.object(jarvis, "speak") as speak:
            jarvis._research("le prix du bitcoin")
        self.assertIn("web", speak.call_args.args[0])

    def test_thanks_never_triggers_a_web_search(self):
        with patch.object(jarvis, "_research") as research, \
                patch.object(jarvis.CONVERSATION, "ask") as ask, \
                patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("merci")
        research.assert_not_called()
        ask.assert_not_called()
        self.assertIn("plaisir", speak.call_args.args[0])

    def test_small_talk_uses_the_configured_first_name(self):
        with patch.object(jarvis, "_research"), patch.object(jarvis, "speak") as speak:
            jarvis.execute_command("bonjour")
        self.assertIn(jarvis.USER_NAME, speak.call_args.args[0])

    def test_bare_transport_words_drive_the_music(self):
        for word, expected in (("pause", "pause"), ("stop", "pause"), ("reprends", "play")):
            with patch.object(jarvis, "_spotify") as spotify, patch.object(jarvis, "speak"):
                jarvis.execute_command(word)
            spotify.assert_called_once_with(expected)

    def test_spoken_lines_keep_their_accents(self):
        # elevenlabs comme chatterbox prononcent « repeter » n'importe comment.
        import re
        source = (Path(jarvis.__file__)).read_text(encoding="utf-8")
        spoken = re.findall(r'speak\(\s*f?"([^"]{4,200})"', source)
        guilty = [line for line in spoken
                  if re.search(r"\b(repeter|desole|ecran|precedent|reussi|deja|apres|tres)\b", line)]
        self.assertEqual(guilty, [])


class TimeQuestionTests(unittest.TestCase):
    """\"heure\" shows up in far more sentences than you'd think."""

    def ask(self, text):
        return jarvis._is_time_question(jarvis._norm(text))

    def test_a_real_time_question_is_recognised(self):
        for phrase in ("quelle heure il est", "il est quelle heure",
                       "donne-moi l'heure", "heure"):
            self.assertTrue(self.ask(phrase), phrase)

    def test_a_duration_is_not_a_time_question(self):
        # this case answered "il est 18 heures 49" instead of starting the timer.
        for phrase in ("rappelle-moi dans un quart d'heure",
                       "minuteur de deux heures",
                       "dans une heure préviens-moi",
                       "mets un chrono de 3 heures"):
            self.assertFalse(self.ask(phrase), phrase)

    def test_an_appointment_question_goes_to_the_agenda(self):
        self.assertFalse(self.ask("à quelle heure est mon rendez-vous"))


class FrenchDurationTests(unittest.TestCase):
    """En français on compte en quarts d'heure avant de compter en minutes."""

    def test_a_quarter_of_an_hour_is_not_an_hour(self):
        seconds, label = jarvis._parse_duration_s(
            jarvis._norm("rappelle-moi dans un quart d'heure"))
        self.assertEqual(seconds, 900)
        self.assertIn("quart", label)

    def test_half_an_hour(self):
        self.assertEqual(jarvis._parse_duration_s(jarvis._norm("minuteur d'une demi-heure"))[0], 1800)

    def test_three_quarters(self):
        self.assertEqual(
            jarvis._parse_duration_s(jarvis._norm("chrono de trois quarts d'heure"))[0], 2700)

    def test_plain_durations_still_work(self):
        self.assertEqual(jarvis._parse_duration_s(jarvis._norm("minuteur de 10 minutes"))[0], 600)
        self.assertEqual(jarvis._parse_duration_s(jarvis._norm("minuteur de 2 heures"))[0], 7200)
        self.assertEqual(jarvis._parse_duration_s(jarvis._norm("30 secondes"))[0], 30)
