"""Policy decisions on labelled numeric histories, without generating art."""
import copy
import unittest

from setforge.acceptance import MAX_EDITS, decide, plateau_signal, policy


def review(quality, eligible=True):
    return {'quality': quality, 'eligible': eligible}


class AcceptancePolicyV2Tests(unittest.TestCase):
    def test_default_is_three_edits_and_independent_snapshot(self):
        snapshot = policy()
        self.assertEqual((snapshot['version'], snapshot['max_edits']), (2, 3))
        self.assertEqual(MAX_EDITS, 3)
        self.assertEqual(snapshot['plateau_action'], 'change_strategy')
        self.assertEqual(snapshot['exhaustion_action'], 'discard')
        snapshot['weights'].clear()
        self.assertTrue(policy()['weights'])

    def test_plateau_does_not_consume_remaining_chance(self):
        history = [review(91), review(90), review(90.5)]
        self.assertTrue(plateau_signal(history))
        self.assertEqual(decide(history), 'revise')
        self.assertEqual(decide(history + [review(93.5)]), 'accepted')

    def test_three_failed_edits_discard_even_with_steady_improvement(self):
        history = [review(89), review(90), review(91), review(92)]
        self.assertFalse(plateau_signal(history))
        self.assertEqual(decide(history), 'discarded')
        self.assertEqual(decide(history[:3]), 'revise')

    def test_threshold_does_not_override_failed_material_gate(self):
        history = [review(94, False)] * 4
        self.assertTrue(plateau_signal(history))
        self.assertEqual(decide(history[:3]), 'revise')
        self.assertEqual(decide(history), 'discarded')

    def test_unrounded_threshold_and_final_edit_acceptance(self):
        self.assertEqual(decide([review(93.499999)]), 'revise')
        self.assertEqual(decide([review(93.5)]), 'accepted')
        self.assertEqual(decide([review(90)] * 3 + [review(93.5)]), 'accepted')

    def test_strategy_signal_restarts_only_on_eligible_gain(self):
        plateau = [review(91), review(100, False), review(99, False)]
        self.assertTrue(plateau_signal(plateau))
        self.assertFalse(plateau_signal(plateau + [review(91.5)]))
        self.assertTrue(plateau_signal(plateau + [review(91.49)]))

    def test_explicit_v1_snapshot_retains_original_stops(self):
        old = policy(1)
        self.assertEqual(old['max_edits'], 5)
        self.assertNotIn('plateau_action', old)
        self.assertEqual(decide([review(91)] * 3, acceptance_policy=old), 'plateau')
        history = [review(89 + n * .6) for n in range(6)]
        self.assertEqual(decide(history[:5], acceptance_policy=old), 'revise')
        self.assertEqual(decide(history, acceptance_policy=old), 'iteration_limit')
        self.assertEqual(decide(history[:5] + [review(93.5)], acceptance_policy=old), 'accepted')

    def test_history_and_snapshot_are_not_mutated(self):
        history, snapshot = [review(91)] * 3, policy(1)
        original = copy.deepcopy((history, snapshot))
        decide(history, acceptance_policy=snapshot)
        plateau_signal(history, acceptance_policy=snapshot)
        self.assertEqual((history, snapshot), original)

    def test_saved_policy_values_are_used(self):
        snapshot = policy()
        snapshot.update(threshold=95, max_edits=2, plateau_nonimproving_edits=1)
        self.assertTrue(plateau_signal([review(91), review(90)], acceptance_policy=snapshot))
        self.assertEqual(decide([review(91), review(94)], acceptance_policy=snapshot), 'revise')
        self.assertEqual(decide([review(91), review(94), review(94)], acceptance_policy=snapshot), 'discarded')
        self.assertEqual(decide([review(95)], acceptance_policy=snapshot), 'accepted')

    def test_invalid_version_or_empty_history_rejected(self):
        for version in (0, 3, True, '1'):
            with self.assertRaises(ValueError):
                policy(version)
        for fn in (decide, plateau_signal):
            with self.assertRaisesRegex(ValueError, 'reviewed image'):
                fn([])


if __name__ == '__main__':
    unittest.main()
