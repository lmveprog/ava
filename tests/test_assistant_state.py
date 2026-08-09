import unittest

from assistant_state import AssistantStateMachine, AvaState, InvalidTransition


class AssistantStateTests(unittest.TestCase):
    def test_nominal_voice_cycle(self):
        machine = AssistantStateMachine()
        machine.transition(AvaState.LISTENING)
        machine.transition(AvaState.THINKING)
        machine.transition(AvaState.ACTING)
        machine.transition(AvaState.SPEAKING)
        machine.dormant()
        self.assertEqual(machine.snapshot.state, AvaState.DORMANT)
        self.assertEqual(machine.snapshot.revision, 5)

    def test_rejects_impossible_transition(self):
        machine = AssistantStateMachine()
        with self.assertRaises(InvalidTransition):
            machine.transition(AvaState.ACTING)

    def test_idle_plugin_can_start_typed_or_voice_work(self):
        machine = AssistantStateMachine()
        machine.idle()
        machine.transition(AvaState.THINKING)
        machine.transition(AvaState.SPEAKING)
        machine.idle()
        machine.transition(AvaState.LISTENING)
        self.assertEqual(machine.snapshot.state, AvaState.LISTENING)


if __name__ == "__main__":
    unittest.main()
