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


if __name__ == "__main__":
    unittest.main()
