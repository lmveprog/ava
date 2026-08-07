import unittest
from unittest.mock import patch

import ava as jarvis
from computer_use import ActionOutcome, ComputerIntent
from conversation import ConversationReply


class CommandRoutingTests(unittest.TestCase):
    def setUp(self):
        jarvis.ASSISTANT_STATE.dormant()
        jarvis.set_assistant_state(jarvis.AvaState.THINKING, "test", force=True)

    def tearDown(self):
        jarvis.ASSISTANT_STATE.dormant()

    def test_keeps_spotify_next_intent(self):
        with patch.object(jarvis, "_spotify") as spotify, patch.object(jarvis, "speak"):
            jarvis.execute_command("morceau suivant")
        spotify.assert_called_once_with("next track")

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

    def test_background_notification_returns_to_dormant(self):
        jarvis.ASSISTANT_STATE.dormant()
        with patch.object(jarvis, "speak") as speak:
            jarvis.notify("Minuteur termine")
        speak.assert_called_once_with("Minuteur termine")
        self.assertEqual(jarvis.ASSISTANT_STATE.snapshot.state, jarvis.AvaState.DORMANT)


if __name__ == "__main__":
    unittest.main()
