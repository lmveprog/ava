"""un seul systeme : local, mesure, sans repli en cascade.

la bascule ne s'est pas decidee sur un principe (« le local c'est mieux ») mais
sur des chiffres pris sur cette machine, meme texte, meme jour :

    moteur                 « Oui ? »    briefing de 31 s   sort du mac
    chatterbox (local)     3-4 s        ~45 s              non
    mistral (reseau)       0,51 s       2,86 s             oui
    kokoro (local, mlx)    0,065 s      1,70 s             non

    transcription          extrait de 11 s de francais
    faster-whisper small   1,85 s   « c'est **Hava** »
    whisper-turbo (mlx)    0,32 s   « c'est **Ava** »

le local a cesse d'etre le repli degrade qu'on subit quand le reseau tombe : il
est devenu le chemin le plus rapide *et* le plus juste. c'est ce qui autorise a
supprimer la pile de replis, pas une preference d'architecture.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import app as ava
from ava.audio import voice_tts as V


class SingleEngineTest(unittest.TestCase):
    def test_le_defaut_ne_depend_de_rien(self):
        """Ni cle, ni quota, ni reseau : c'est tout l'objet de la bascule.

        Le timbre a ete tranche a l'oreille (chatterbox), la dependance ne se
        negocie pas : quel que soit le moteur retenu, il tourne sur le mac.
        """
        with patch.object(V, "_settings", return_value={}):
            self.assertIn(V.engine_name(), {"chatterbox", "kokoro"})
        self.assertNotIn(V.DEFAULT_ENGINE, {"mistral", "elevenlabs"})

    def test_la_cascade_de_replis_n_existe_plus(self):
        """Quatre moteurs enchaines, c'etaient quatre timbres possibles pour une
        meme phrase et quatre timeouts a payer avant d'arriver au dernier."""
        with patch.object(V, "_settings", return_value={"engine": "kokoro"}), \
                patch.object(V, "_kokoro_audio", return_value=None), \
                patch.object(V, "_mistral_audio") as distant, \
                patch.object(V, "_chatterbox_audio") as chatter, \
                patch.object(V, "_eleven_audio") as eleven:
            self.assertIsNone(V.synthesize("Bonjour"))
        for moteur in (distant, chatter, eleven):
            moteur.assert_not_called()

    def test_le_texte_long_est_decoupe_avant_la_synthese(self):
        """⚠️ le g2p francais de kokoro passe par espeak, qui **tronque** les
        longs textes faute de decoupage interne. sans ce decoupage, la fin du
        briefing disparaissait purement et simplement."""
        long_texte = " ".join(f"Phrase numéro {n} du briefing." for n in range(1, 21))
        unites = V.split_speech_units(long_texte)
        self.assertGreater(len(unites), 1)
        self.assertTrue(all(len(u) <= V.MAX_CHUNK_CHARS for u in unites))


class LocalTranscriptionTest(unittest.TestCase):
    def test_le_gpu_passe_avant_le_reseau(self):
        with patch.object(ava, "mlx_transcribe", return_value="ouvre spotify") as local, \
                patch.object(ava, "voxtral_transcribe") as distant:
            self.assertEqual(ava.transcribe(b"\x00\x01"), "ouvre spotify")
        local.assert_called_once()
        distant.assert_not_called()

    def test_voxtral_ne_repart_que_si_on_le_demande(self):
        """Il sort du mac : il ne doit pas se rallumer tout seul."""
        with patch.object(ava, "mlx_transcribe", return_value=""), \
                patch.object(ava, "voxtral_transcribe") as distant, \
                patch.object(ava, "get_whisper") as whisper, \
                patch.dict(ava.os.environ, {}, clear=False):
            ava.os.environ.pop("AVA_USE_VOXTRAL", None)
            whisper.return_value.transcribe.return_value = ([], None)
            ava.transcribe(b"\x00\x01")
        distant.assert_not_called()

    def test_mlx_absent_ne_se_reessaie_pas_a_chaque_phrase(self):
        """Sur une machine sans apple silicon, reessayer l'import a chaque
        commande coutait le prix de l'echec, a chaque fois."""
        ava._mlx_whisper_ok = True
        self.addCleanup(setattr, ava, "_mlx_whisper_ok", True)
        with patch.dict(sys.modules, {"mlx_whisper": None}):
            self.assertIsNone(ava.mlx_transcribe(b"\x00\x01"))
        self.assertFalse(ava._mlx_whisper_ok)


class BargeInTest(unittest.TestCase):
    """On doit pouvoir la couper : un briefing de trente secondes etait insecable."""

    def setUp(self):
        ava._player = None
        self.addCleanup(setattr, ava, "_player", None)

    def test_rien_a_couper_quand_elle_se_tait(self):
        self.assertFalse(ava.stop_speaking())
        self.assertFalse(ava.speaking())

    def test_couper_arrete_le_lecteur(self):
        class Lecteur:
            def __init__(self):
                self.tue = False

            def poll(self):
                return None if not self.tue else 0

            def terminate(self):
                self.tue = True

        lecteur = Lecteur()
        ava._player = lecteur
        self.assertTrue(ava.speaking())
        self.assertTrue(ava.stop_speaking())
        self.assertTrue(lecteur.tue)

    def test_le_mot_stop_la_fait_taire_au_lieu_de_lancer_une_commande(self):
        with patch.object(ava, "stop_speaking", return_value=True) as couper, \
                patch.object(ava, "_reserve_interaction") as reserver:
            reponse = ava.submit_text_command("stop")
        couper.assert_called_once()
        reserver.assert_not_called()
        self.assertTrue(reponse.get("interrupted"))

    def test_cliquer_le_micro_pendant_qu_elle_parle_la_coupe(self):
        with patch.object(ava, "stop_speaking") as couper, \
                patch.object(ava, "_reserve_interaction", return_value=False):
            ava.start_voice_interaction()
        couper.assert_called_once()


class OverlayContractTest(unittest.TestCase):
    """Le pont python -> js doit rester complet des deux cotes."""

    def test_l_interruption_existe_de_bout_en_bout(self):
        from ava.ui import overlay as overlay
        self.assertTrue(hasattr(overlay, "interrupted"))
        page = (Path(__file__).resolve().parents[1] / "src" / "ava" / "ui" / "web" / "ava.html").read_text(
            encoding="utf-8")
        self.assertIn("window.avaInterrupted", page)
        # ce qui n'a pas ete prononce doit se distinguer de ce qui l'a ete.
        self.assertIn("unsaid", page)


if __name__ == "__main__":
    unittest.main()
