"""Refinement reviews honor saved policies without image/model invocations."""
import tempfile
import unittest
from pathlib import Path

from setforge.acceptance import IDS, policy
from setforge.refine import read, review, saved_policy, status, write


class RefinePolicyV2Tests(unittest.TestCase):
    def test_old_scalar_state_resolves_to_v1_without_mutation(self):
        state = {'threshold': 93.5, 'max_edits': 5, 'plateau_delta': .25}
        snapshot = saved_policy(state)
        self.assertEqual(snapshot['version'], 1)
        self.assertEqual(snapshot['max_edits'], 5)
        self.assertEqual(snapshot['plateau_delta'], .25)
        self.assertNotIn('art_acceptance', state)

    def run_reviews(self, snapshot, count):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = Path(temp.name)
        baseline = {'iteration': 0, 'image': 'baseline.png', 'sha256': 'baseline',
                    'quality': 90, 'eligible': True}
        state = {'status': 'running', 'pending': None, 'threshold': 93.5,
                 'max_edits': 5, 'cards': {'card': {'title': 'Card', 'history': [baseline],
                 'best': 0, 'status': 'revise', 'next_feedback': 'TEST feedback',
                 'next_strategy': 'TEST broad matte shapes'}}}
        if snapshot is not None:
            state['art_acceptance'] = snapshot
        answer = {'scores': dict.fromkeys(IDS, 90),
                  'evidence': dict.fromkeys(IDS, 'TEST numeric fixture only'),
                  'blockers': [], 'class_pass': True, 'canon_pass': True,
                  'full_inspected': True, 'thumbnail_inspected': True,
                  'summary': 'TEST fixture', 'feedback': 'TEST simplify surface',
                  'next_strategy': 'TEST rebuild broad material planes'}
        write(run / 'answer.json', answer)
        for iteration in range(1, count + 1):
            folder = run / 'cards' / 'card' / f'iteration-{iteration:02d}'
            folder.mkdir(parents=True)
            (folder / 'native.png').write_bytes(b'TEST fixture evidence: not generated art')
            state['pending'] = {'kind': 'review', 'job_id': f'edit-{iteration}',
                                'card': 'card', 'iteration': iteration,
                                'request': (folder / 'request.json').relative_to(run).as_posix()}
            write(run / 'state.json', state)
            review(run, f'edit-{iteration}', run / 'answer.json')
            state = read(run / 'state.json')
        return run, state

    def test_v2_plateau_signal_continues_then_discards_at_three_edits(self):
        run, state = self.run_reviews(policy(), 2)
        card = state['cards']['card']
        self.assertEqual(card['status'], 'revise')
        self.assertTrue(card['plateau_signal'])
        self.assertTrue(card['history'][-1]['plateau_signal'])
        self.assertFalse(status(run)['all_accepted'])
        run, state = self.run_reviews(policy(), 3)
        self.assertEqual(state['cards']['card']['status'], 'discarded')
        self.assertEqual(len(state['cards']['card']['history']), 4)
        self.assertEqual(status(run)['cards']['card']['edits'], 3)
        self.assertFalse(status(run)['all_accepted'])

    def test_legacy_review_retains_early_plateau_stop(self):
        run, state = self.run_reviews(None, 2)
        self.assertEqual(state['cards']['card']['status'], 'plateau')
        self.assertEqual(status(run)['art_acceptance']['version'], 1)
        self.assertEqual(status(run)['art_acceptance']['max_edits'], 5)
        self.assertNotIn('art_acceptance', state)


if __name__ == '__main__':
    unittest.main()
