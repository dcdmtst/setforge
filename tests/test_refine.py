import unittest
from setforge.refine import decide as current_decide,score,IDS
from setforge.acceptance import policy

# Explicit legacy snapshots preserve their early-stop contract. New v2
# strategy/discard behavior is covered in test_refine_policy_v2.py.
def decide(history):return current_decide(history,acceptance_policy=policy(1))
def h(q,eligible=True):return {'quality':q,'eligible':eligible}
class RefinementTests(unittest.TestCase):
 def test_exact_threshold_and_blocker(self):
  self.assertEqual(decide([h(93.5)]),'accepted')
  self.assertEqual(decide([h(99,False)]),'revise')
 def test_no_rounded_acceptance(self):self.assertEqual(decide([h(93.49)]),'revise')
 def test_best_score_not_latest_controls_stagnation(self):
  self.assertEqual(decide([h(91),h(92),h(91),h(91.9)]),'plateau')
 def test_ineligible_gain_does_not_reset_plateau(self):
  self.assertEqual(decide([h(91),h(99,False),h(100,False)]),'plateau')
 def test_plateau_keeps_best_and_requires_two_attempts(self):
  self.assertEqual(decide([h(91),h(90)]),'revise')
  self.assertEqual(decide([h(91),h(90),h(90.5)]),'plateau')
 def test_improvement_restarts_plateau(self):self.assertEqual(decide([h(91),h(90),h(92)]),'revise')
 def test_limit_does_not_mean_pass(self):self.assertEqual(decide([h(90+i*.6) for i in range(6)]),'iteration_limit')
 def test_formula_and_step(self):
  self.assertEqual(score(dict.fromkeys(IDS,95)),95)
  with self.assertRaises(ValueError):score(dict.fromkeys(IDS,94))
