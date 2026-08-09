"""la passe de fumee, devenue test : des phrases vraies, la route attendue.

les trous corriges ici n'ont pas ete trouves en lisant le code mais en faisant
passer une cinquantaine de tournures reelles dans `execute_command` avec le
reseau coupe et les effets de bord bloques. tous rendaient la meme chose —
« je n'ai pas trouve de source suffisamment claire » — parce que le filet de la
recherche web attrape tout ce que le routage laisse passer, y compris « salut »
ou « precedent ». on verifie donc la **route**, pas la phrase rendue.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ava
import net
from conversation import ConversationReply


class RoutingGapsTest(unittest.TestCase):
    """Chaque cas ci-dessous partait en recherche web avant correction."""

    def setUp(self):
        ava.ASSISTANT_STATE.dormant()
        ava.set_assistant_state(ava.AvaState.THINKING, "test", force=True)
        net.reset()
        self.addCleanup(net.reset)
        self.addCleanup(ava.ASSISTANT_STATE.dormant)

    def run_phrase(self, phrase: str):
        """Joue une phrase, rend (ce qui a ete dit, les appels observes)."""
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

    # --- agenda dit sans le mot « agenda » ----------------------------------

    def test_l_agenda_se_demande_sans_dire_agenda(self):
        for phrase in ("qu'est-ce que j'ai aujourd'hui",
                       "c'est quoi mon programme demain",
                       "à quelle heure est mon rendez-vous",
                       "montre-moi mon planning",
                       "j'ai quoi de prévu ce soir"):
            with self.subTest(phrase=phrase):
                _said, calls = self.run_phrase(phrase)
                calls["agenda"].assert_called_once()
                self.assertNoResearch(calls, phrase)

    def test_ajouter_un_rendez_vous_reste_une_ecriture(self):
        """« ajoute un rdv » contient « rendez vous » : l'ecriture passe avant."""
        with patch.object(ava, "_calendar_create") as create, \
                patch.object(ava, "_calendar_summary") as summary, \
                patch.object(ava, "speak"):
            ava.execute_command("ajoute un rendez-vous dentiste demain à 14h")
        create.assert_called_once()
        summary.assert_not_called()

    # --- transport dit tout seul --------------------------------------------

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

    def test_ia_ne_s_attrape_pas_dans_un_autre_mot(self):
        """« ia » cherche en mot entier : « diaporama » n'est pas de l'actu."""
        _said, calls = self.run_phrase("ouvre le diaporama")
        calls["news"].assert_not_called()

    # --- le nom, les hesitations, les phrases coupees ------------------------

    def test_le_nom_en_fin_de_phrase(self):
        said, calls = self.run_phrase("salut ava")
        self.assertIn("Salut", said)
        self.assertNoResearch(calls, "salut ava")

    def test_ca_va_n_est_pas_le_nom(self):
        """« va » n'est qu'a une lettre d'« ava » : la ressemblance mangeait
        la moitie de la phrase, et « ça va » devenait « ça »."""
        said, _calls = self.run_phrase("ça va")
        self.assertIn("Ça va", said)

    def test_le_nom_dit_seul_est_un_appel(self):
        said, calls = self.run_phrase("ava")
        self.assertIn("Oui", said)
        self.assertNoResearch(calls, "ava")

    def test_une_hesitation_ne_lance_pas_de_recherche(self):
        said, calls = self.run_phrase("euh")
        self.assertNoResearch(calls, "euh")
        self.assertTrue(said)

    def test_un_verbe_sans_complement_redemande(self):
        for phrase, expected in (("ouvre", "Ouvrir quoi"),
                                 ("rappelle-moi", "Te rappeler quoi")):
            with self.subTest(phrase=phrase):
                said, calls = self.run_phrase(phrase)
                self.assertIn(expected, said)
                self.assertNoResearch(calls, phrase)

    def test_fermer_ce_que_le_rituel_a_ouvert(self):
        said, calls = self.run_phrase("ferme tout")
        calls["close"].assert_called_once()
        self.assertIn("ferme", said.lower())

    # --- hors ligne : dire la vraie raison -----------------------------------

    def test_hors_ligne_ava_dit_qu_elle_n_a_pas_de_reseau(self):
        """Avant, elle disait avoir cherche sans trouver de source claire —
        alors qu'elle n'etait meme pas sortie du mac."""
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
        from google_calendar import GoogleEvent
        # demain : tout est « a venir » quelle que soit l'heure du test.
        base = dt.datetime.now() + dt.timedelta(days=1)
        events = tuple(
            GoogleEvent(id=str(hour), title=f"rendez-vous {index}",
                        start=base.replace(hour=hour, minute=0, second=0, microsecond=0),
                        end=base.replace(hour=hour, minute=30, second=0, microsecond=0))
            for index, hour in enumerate(hours, 1))
        with patch.object(ava, "agenda_events", return_value=(events, "google")):
            return ava.calendar_sentence()

    def test_une_journee_chargee_reste_courte(self):
        said = self.sentence([9, 11, 14, 15, 16, 18, 20])
        self.assertIn("7 rendez-vous", said)
        self.assertIn("Et 4 autres ensuite", said)
        self.assertNotIn("rendez-vous 4", said)      # le 4e n'est pas enumere
        self.assertLess(len(said.split()), 30)

    def test_pas_de_reste_pas_de_queue(self):
        said = self.sentence([9, 11, 14])
        self.assertNotIn("ensuite", said)

    def test_un_seul_rendez_vous_ne_s_annonce_pas_comme_une_liste(self):
        said = self.sentence([11])
        self.assertIn("un seul rendez-vous", said)

    def test_l_agenda_vide_se_dit_positivement(self):
        said = self.sentence([])
        self.assertIn("vide", said)


class AmbientSpeechTest(unittest.TestCase):
    """Le micro entend la piece entiere, pas seulement Matheus.

    Les trois premieres phrases sortent telles quelles du journal du 8 aout :
    un « salut ava » a ouvert l'ecoute pendant un match a la television, et Ava
    est partie chercher « Thibaut Delphis et les Anéciens » sur le web, puis a
    enchaine deux relances sur le commentaire.
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

    def test_un_recit_long_passe_pas_meme_avec_un_mot_interrogatif(self):
        """Relevé en vrai : « ce que j'adore c'est **quand** on est dans la
        baignoire » venait d'une vidéo, et « quand » l'a fait passer pour une
        question. Quarante mots en deux phrases, c'est du récit."""
        for phrase in self.RECIT:
            with self.subTest(phrase=phrase[:40]):
                self.assertTrue(ava.looks_ambient(phrase))

    def test_le_commentaire_de_la_television_est_ecarte(self):
        for phrase in self.AMBIANCE:
            with self.subTest(phrase=phrase[:40]):
                self.assertTrue(ava.looks_ambient(phrase))

    def test_une_vraie_commande_passe_meme_longue(self):
        for phrase in self.COMMANDES:
            with self.subTest(phrase=phrase[:40]):
                self.assertFalse(ava.looks_ambient(phrase))

    def test_un_suivi_d_ambiance_referme_sans_repondre(self):
        with patch.object(ava, "execute_command") as run, \
                patch.object(ava, "transcribe", return_value=self.AMBIANCE[1]), \
                patch.object(ava, "_record_utterance", return_value=b""), \
                patch.object(ava, "speak"), patch.object(ava, "ui"), \
                patch.object(ava, "set_assistant_state"), \
                patch.object(ava, "_drain_command_queue"), \
                patch.object(ava, "_drain_wake_queue"):
            ava._run_continuous_conversation("quelle heure est-il", display_initial=False)
        # un seul tour : la commande initiale, pas le commentaire du match.
        self.assertEqual(run.call_count, 1)


class TimerTest(unittest.TestCase):
    def test_un_minuteur_ne_retient_pas_ava_au_moment_de_quitter(self):
        """`threading.Timer` fabrique un thread non-daemon : un minuteur de
        trente minutes tenait le process en vie trente minutes, et « quitter »
        semblait ne rien faire."""
        timer = ava.start_timer(3600, "test")
        self.addCleanup(timer.cancel)
        self.assertTrue(timer.daemon)


class SpokenAccentsTest(unittest.TestCase):
    """Ce qui est dit a voix haute porte ses accents.

    Exigence explicite de Matheus : la synthese vocale prononce « note » et
    « noté », « lance » et « lancé » differemment — un accent manquant s'entend,
    la ou il ne se voit pas dans le code.
    """

    # participes passes et noms qui perdent leur accent sans que ca se voie.
    SUSPECTS = ("Meteo", "est termine", "Action terminee", "lance pour",
                "C'est note", "enregistree", "a ete arretee", "de l'ecran",
                "termine deja")

    FILES = ("ava.py", "computer_use.py", "web_research.py",
             "google_calendar.py", "skills.py")

    @classmethod
    def spoken_lines(cls, root: Path):
        """Les lignes qui fabriquent une phrase destinee a la voix."""
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

    def test_aucune_phrase_parlee_sans_accent(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(self.offenders(root), [])

    def test_le_garde_fou_attrape_bien_une_regression(self):
        """Un test de forme qui ne peut pas echouer ne protege de rien."""
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in self.FILES:
                (root / name).write_text("", encoding="utf-8")
            (root / "ava.py").write_text('    speak("C\'est note.")\n', encoding="utf-8")
            self.assertTrue(self.offenders(root))


if __name__ == "__main__":
    unittest.main()
