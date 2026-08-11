"""the smoke pass, turned into a test: real phrasings, the expected route.

the gaps fixed here weren't found by reading the code but by running fifty-odd
real phrasings through `execute_command` with the network down and the side
effects blocked. every one of them came back with the same thing — "je n'ai pas
trouve de source suffisamment claire" — because the web search net catches
everything the routing lets through, "salut" and "precedent" included. so we
check the **route**, not the sentence produced.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import app as ava
from ava import net as net
from ava.brain.conversation import ConversationReply


class RoutingGapsTest(unittest.TestCase):
    """Chaque cas ci-dessous partait en recherche web avant correction."""

    def setUp(self):
        ava.ASSISTANT_STATE.dormant()
        ava.set_assistant_state(ava.AvaState.THINKING, "test", force=True)
        net.reset()
        self.addCleanup(net.reset)
        self.addCleanup(ava.ASSISTANT_STATE.dormant)
        # a throwaway vault: smoke phrases must never touch the real memory.
        import tempfile
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch
        from ava.services.obsidian import ObsidianMemory
        vault = tempfile.TemporaryDirectory()
        self.addCleanup(vault.cleanup)
        memory = _patch.object(ava, "MEMORY", ObsidianMemory(_Path(vault.name)))
        memory.start()
        self.addCleanup(memory.stop)

    def run_phrase(self, phrase: str):
        """Run a phrasing, return (what was said, the calls observed)."""
        with patch.object(ava, "speak") as speak, \
                patch.object(ava, "_spotify") as spotify, \
                patch.object(ava, "_calendar_summary") as agenda, \
                patch.object(ava, "ai_news_sentence", return_value="Une actu.") as news, \
                patch.object(ava, "_research") as research, \
                patch.object(ava, "close_startup_apps", return_value=["Notes"]) as closing, \
                patch.object(ava, "_dispatch_understood", return_value=False), \
                patch.object(ava.CONVERSATION, "ask",
                             return_value=ConversationReply(False)), \
                patch.object(ava, "ui"), patch.object(ava, "_osascript"):
            ava.execute_command(phrase)
        said = " ".join(str(call.args[0]) for call in speak.call_args_list)
        return said, {"spotify": spotify, "agenda": agenda, "news": news,
                      "research": research, "close": closing}

    def assertNoResearch(self, calls, phrase):
        calls["research"].assert_not_called()

    # --- the calendar, asked for without the word "agenda" ------------------

    def test_the_calendar_is_asked_for_without_saying_agenda(self):
        for phrase in ("qu'est-ce que j'ai aujourd'hui",
                       "c'est quoi mon programme demain",
                       "à quelle heure est mon rendez-vous",
                       "montre-moi mon planning",
                       "j'ai quoi de prévu ce soir"):
            with self.subTest(phrase=phrase):
                _said, calls = self.run_phrase(phrase)
                calls["agenda"].assert_called_once()
                self.assertNoResearch(calls, phrase)

    def test_adding_an_event_stays_a_write(self):
        """« ajoute un rdv » contient « rendez vous » : l'ecriture passe avant."""
        with patch.object(ava, "_calendar_create") as create, \
                patch.object(ava, "_calendar_summary") as summary, \
                patch.object(ava, "speak"):
            ava.execute_command("ajoute un rendez-vous dentiste demain à 14h")
        create.assert_called_once()
        summary.assert_not_called()

    # --- transport words said on their own ----------------------------------

    def test_suivant_et_precedent_seuls(self):
        _said, calls = self.run_phrase("précédent")
        calls["spotify"].assert_called_once_with("previous track")
        _said, calls = self.run_phrase("suivant")
        calls["spotify"].assert_called_once_with("next track")

    # --- actu ---------------------------------------------------------------

    def test_quoi_de_neuf_en_ia(self):
        for phrase in ("quoi de neuf en IA", "des nouvelles de l'IA ?"):
            with self.subTest(phrase=phrase):
                _said, calls = self.run_phrase(phrase)
                calls["news"].assert_called_once()

    def test_ia_is_not_caught_inside_another_word(self):
        """« ia » cherche en mot entier : « diaporama » n'est pas de l'actu."""
        _said, calls = self.run_phrase("ouvre le diaporama")
        calls["news"].assert_not_called()

    # --- the name, hesitations, and sentences cut short ---------------------

    def test_the_name_at_the_end_of_a_sentence(self):
        said, calls = self.run_phrase("salut ava")
        self.assertIn("Salut", said)
        self.assertNoResearch(calls, "salut ava")

    def test_ca_va_is_not_the_name(self):
        """\"va\" is one letter from \"ava\": the resemblance ate half the
        sentence, and \"ça va\" turned into \"ça\"."""
        said, _calls = self.run_phrase("ça va")
        self.assertIn("Ça va", said)

    def test_the_name_said_alone_is_someone_calling(self):
        said, calls = self.run_phrase("ava")
        self.assertIn("Oui", said)
        self.assertNoResearch(calls, "ava")

    def test_a_hesitation_starts_no_search(self):
        said, calls = self.run_phrase("euh")
        self.assertNoResearch(calls, "euh")
        self.assertTrue(said)

    def test_a_verb_with_no_object_asks_again(self):
        for phrase, expected in (("ouvre", "Ouvrir quoi"),
                                 ("rappelle-moi", "Te rappeler quoi")):
            with self.subTest(phrase=phrase):
                said, calls = self.run_phrase(phrase)
                self.assertIn(expected, said)
                self.assertNoResearch(calls, phrase)

    def test_closing_what_the_ritual_opened(self):
        said, calls = self.run_phrase("ferme tout")
        calls["close"].assert_called_once()
        self.assertIn("ferme", said.lower())

    # --- offline: say the real reason ---------------------------------------

    def test_offline_ava_says_she_has_no_network(self):
        """She used to say she'd searched and found no clear source — when she
        hadn't even left the mac."""
        net.note_failure("test", ConnectionError("coupé"))
        with patch.object(ava, "speak") as speak, patch.object(ava, "ui"), \
                patch.object(ava.WEB_RESEARCH, "answer") as answer:
            ava._research("le prochain match de l'OM")
        answer.assert_not_called()
        self.assertIn("réseau", " ".join(str(c.args[0]) for c in speak.call_args_list))


class AgendaSentenceTest(unittest.TestCase):
    """Le compte d'abord, les trois prochains, puis le reste en nombre."""

    def sentence(self, hours):
        import datetime as dt
        from ava.services.google_calendar import GoogleEvent
        # tomorrow: everything is upcoming whatever time the test runs.
        base = dt.datetime.now() + dt.timedelta(days=1)
        events = tuple(
            GoogleEvent(id=str(hour), title=f"rendez-vous {index}",
                        start=base.replace(hour=hour, minute=0, second=0, microsecond=0),
                        end=base.replace(hour=hour, minute=30, second=0, microsecond=0))
            for index, hour in enumerate(hours, 1))
        with patch.object(ava, "agenda_events", return_value=(events, "google")):
            return ava.calendar_sentence()

    def test_a_busy_day_stays_short(self):
        said = self.sentence([9, 11, 14, 15, 16, 18, 20])
        self.assertIn("7 rendez-vous", said)
        self.assertIn("Et 4 autres ensuite", said)
        self.assertNotIn("rendez-vous 4", said)      # le 4e n'est pas enumere
        self.assertLess(len(said.split()), 30)

    def test_no_remainder_means_no_tail(self):
        said = self.sentence([9, 11, 14])
        self.assertNotIn("ensuite", said)

    def test_a_single_event_is_not_announced_as_a_list(self):
        said = self.sentence([11])
        self.assertIn("un seul rendez-vous", said)

    def test_an_empty_calendar_is_said_positively(self):
        said = self.sentence([])
        self.assertIn("vide", said)


class AmbientSpeechTest(unittest.TestCase):
    """The mic hears the whole room, not just the person talking to her.

    The first three phrasings come straight out of the log for 8 august: a
    "salut ava" opened the mic during a match on television, and Ava went off to
    search the web for "Thibaut Delphis et les Anéciens", then followed up twice
    more on the commentary.
    """

    AMBIANCE = (
        "Thibaut Delphis. Même s'il n'arrive plus à se relancer. Désormais, les Anéciens",
        "Versini, Versini face à Escalès. La sortie gagnante. Putain. De Florian "
        "Escalès. Allez, allez Joe, allez Joe. Ah enfin, de l'animation dans "
        "cette rencontre. La sortie gagnante.",
        "Tu connais Giovanni Versini ? Versini se remet dans le sens du jeu. "
        "Bamba. Il y a été empaqué.",
    )

    COMMANDES = (
        "ouvre spotify",
        "quelle heure est-il",
        "explique-moi comment fonctionne le mécanisme d'attention dans les transformers",
        "ajoute un rendez-vous chez le dentiste demain à 14 heures dans mon agenda",
        "rappelle-moi dans un quart d'heure d'appeler le comptable s'il te plaît",
        "c'est quand le prochain match de l'OM au Vélodrome cette saison",
    )

    RECIT = (
        "Et par ici, la salle de bain, avec un masque séparé, la baignoire avec "
        "bain à remont. Ce que j'adore c'est quand on est dans la baignoire, "
        "c'est une télé d'ailleurs, si vous voulez me prélasser et que vous "
        "souhaitez appuyer sur le panneau avec le bouton",
    )

    def test_a_long_story_does_not_pass_even_with_a_question_word(self):
        """Straight from real life: "ce que j'adore c'est **quand** on est dans
        la baignoire" came from a video, and "quand" made it look like a
        question. Forty words over two sentences is a story."""
        for phrase in self.RECIT:
            with self.subTest(phrase=phrase[:40]):
                self.assertTrue(ava.looks_ambient(phrase))

    def test_television_commentary_is_dropped(self):
        for phrase in self.AMBIANCE:
            with self.subTest(phrase=phrase[:40]):
                self.assertTrue(ava.looks_ambient(phrase))

    def test_a_real_command_gets_through_even_a_long_one(self):
        for phrase in self.COMMANDES:
            with self.subTest(phrase=phrase[:40]):
                self.assertFalse(ava.looks_ambient(phrase))

    def test_an_ambient_followup_closes_without_answering(self):
        with patch.object(ava, "execute_command") as run, \
                patch.object(ava, "transcribe", return_value=self.AMBIANCE[1]), \
                patch.object(ava, "_record_utterance", return_value=b""), \
                patch.object(ava, "speak"), patch.object(ava, "ui"), \
                patch.object(ava, "set_assistant_state"), \
                patch.object(ava, "_drain_command_queue"), \
                patch.object(ava, "_drain_wake_queue"):
            ava._run_continuous_conversation("quelle heure est-il", display_initial=False)
        # one turn only: the initial command, not the match commentary.
        self.assertEqual(run.call_count, 1)


class TimerTest(unittest.TestCase):
    def test_a_timer_does_not_hold_ava_back_when_quitting(self):
        """`threading.Timer` makes a non-daemon thread: a thirty-minute timer
        kept the process alive for thirty minutes, and "quit" looked like it
        did nothing."""
        timer = ava.start_timer(3600, "test")
        self.addCleanup(timer.cancel)
        self.assertTrue(timer.daemon)


class SpokenAccentsTest(unittest.TestCase):
    """Anything said out loud keeps its accents.

    A hard requirement: the synthesiser pronounces "note" and "noté", "lance"
    and "lancé" differently — a missing accent is audible, where it goes unseen
    in the code.
    """

    # past participles and nouns that lose their accent without it showing.
    SUSPECTS = ("Meteo", "est termine", "Action terminee", "lance pour",
                "C'est note", "enregistree", "a ete arretee", "de l'ecran",
                "termine deja")

    FILES = ("app.py", "mac/computer_use.py", "services/web_research.py",
             "services/google_calendar.py", "brain/skills.py")

    @classmethod
    def spoken_lines(cls, root: Path):
        """The lines that build a sentence destined for the voice."""
        for name in cls.FILES:
            for number, line in enumerate(
                    (root / name).read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#") or '"' not in line:
                    continue
                if any(marker in line for marker in
                       ("speak(", "summary=", "ActionOutcome(", "return f\"", "\"error\":")):
                    yield f"{name}:{number}", line

    def offenders(self, root: Path) -> list[str]:
        return [f"{where} {line.strip()[:80]}"
                for where, line in self.spoken_lines(root)
                for suspect in self.SUSPECTS if suspect in line]

    def test_no_spoken_line_is_missing_its_accents(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ava"
        self.assertEqual(self.offenders(root), [])

    def test_the_guard_really_catches_a_regression(self):
        """A shape test that cannot fail protects nothing."""
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in self.FILES:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            (root / "app.py").write_text('    speak("C\'est note.")\n', encoding="utf-8")
            self.assertTrue(self.offenders(root))


if __name__ == "__main__":
    unittest.main()
