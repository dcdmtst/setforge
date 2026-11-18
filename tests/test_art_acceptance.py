"""Numeric gate behavior only: labelled offline fixtures are never real art QA."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from setforge.acceptance import IDS, policy, score, decide
from setforge.cli import main
from setforge.engine import Pipeline, NeedsInput
from setforge.io import read, write
from setforge.report import export, report
from fixture_worker import review
import test_pipeline as legacy


class ArtAcceptanceTests(unittest.TestCase):
    setUp = legacy.PipelineTests.setUp
    make = legacy.PipelineTests.make
    wait = legacy.PipelineTests.wait
    deliver = legacy.PipelineTests.deliver
    to_images = legacy.PipelineTests.to_images

    def candidate_review(self, pipe, scores=None, **changes):
        self.deliver(pipe)  # Supply the pending labelled image fixture.
        request = self.wait(pipe)
        self.assertEqual(request['kind'], 'card_review')
        result = review(request)
        result['art_review'].update(scores=scores or dict.fromkeys(IDS, 90),
                                    feedback='TEST: fix material planes',
                                    next_strategy='TEST: broader matte planes')
        result['art_review'].update(changes)
        return request, result

    def test_new_run_policy_and_low_pass_cannot_accept_or_export(self):
        pipe = self.make()
        self.assertEqual(read(self.run/'inputs/acceptance-policy.json'), policy())
        self.to_images(pipe)
        _, result = self.candidate_review(pipe)
        pipe.accept(result, 'test-fixture', 'not-a-real-model')
        self.assertFalse(pipe.state['accepted'])
        self.assertFalse(pipe.state['canon'])
        self.assertEqual(pipe.state['phase'], 'next_card')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            export(self.run, pipe.state, pipe.config)
        report(self.run, pipe.state)
        self.assertIn('93,5', (self.run/'report.html').read_text(encoding='utf-8'))

    def test_unrounded_score_and_missing_feedback(self):
        pipe = self.make()
        self.to_images(pipe)
        scores = dict.fromkeys(IDS, 95)
        scores.update(background=80, lighting=90)
        self.assertLess(score(scores), 93.5)
        self.assertEqual(round(score(scores), 1), 93.5)
        _, result = self.candidate_review(pipe, scores, feedback='')
        with self.assertRaisesRegex(ValueError, 'feedback'):
            pipe.accept(result)
        result['art_review']['feedback'] = 'TEST: simplify background'
        pipe.accept(result)
        self.assertFalse(pipe.state['accepted'])

    def test_visual_inspection_and_derived_score_cannot_be_spoofed(self):
        pipe = self.make()
        self.to_images(pipe)
        _, result = self.candidate_review(pipe, dict.fromkeys(IDS, 95), full_inspected=False)
        with self.assertRaisesRegex(ValueError, 'inspection'):
            pipe.accept(result)
        result['art_review']['full_inspected'] = True
        result['art_review']['quality'] = 100
        with self.assertRaisesRegex(ValueError, 'Additional properties'):
            pipe.accept(result)

    def test_high_scores_do_not_bypass_class_or_canon(self):
        pipe = self.make()
        self.to_images(pipe)
        _, result = self.candidate_review(pipe, dict.fromkeys(IDS, 100), class_pass=False)
        pipe.accept(result)
        self.assertFalse(pipe.state['accepted'])
        self.assertFalse(pipe.state['art_history']['v1/c1'][-1]['eligible'])
        self.wait(pipe)
        _, result = self.candidate_review(pipe, dict.fromkeys(IDS, 100), canon_pass=False)
        pipe.accept(result)
        self.assertFalse(pipe.state['accepted'])

    def test_plateau_cli_exit_and_continue_cannot_override(self):
        with patch('setforge.engine.art_policy',return_value=policy(1)):
            pipe = self.make()
        self.to_images(pipe)
        for n in range(3):
            request, result = self.candidate_review(pipe)
            if n < 2:
                pipe.accept(result)
                self.wait(pipe)
            else:
                response = self.root/'answer.json'
                write(response, result)
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = main(['submit', '--run', str(self.run), '--job', request['job_id'],
                               '--response', str(response), '--executor', 'test-fixture'])
                self.assertEqual(rc, 3)
        pipe = Pipeline(self.run)
        self.assertEqual(pipe.state['art_stops']['v1/c1'], 'plateau')
        self.assertEqual(pipe.state['phase'], 'art_stopped')
        self.assertFalse(pipe.state['accepted'])
        with self.assertRaisesRegex(ValueError, 'cannot override'):
            pipe.resume_attention('Approve anyway', {'max_attempts_per_task': 50})
        self.assertFalse((self.run/'exports').exists())

    def test_five_edits_not_three_attempts_and_no_sixth_edit(self):
        with patch('setforge.engine.art_policy',return_value=policy(1)):
            pipe = self.make()
        self.to_images(pipe)
        scores = dict.fromkeys(IDS, 90)
        for n in range(6):
            if n:
                scores[['technical','palette','narrative','style','background'][n-1]] = 95
            _, result = self.candidate_review(pipe, copy.deepcopy(scores))
            if n < 5:
                pipe.accept(result)
                self.wait(pipe)
            else:
                with self.assertRaises(NeedsInput):
                    pipe.accept(result)
        self.assertEqual(pipe.state['art_stops']['v1/c1'], 'iteration_limit')
        self.assertEqual(len(pipe.state['art_history']['v1/c1']), 6)
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'], 6)
        calls = pipe.state['calls']
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertEqual(pipe.state['calls'], calls)
        self.assertFalse(pipe.state['accepted'])

    def test_threshold_on_fifth_edit_wins_over_limit(self):
        history = [{'quality': 89+n*.6, 'eligible': True} for n in range(5)]
        self.assertEqual(decide(history+[{'quality':93.5,'eligible':True}],acceptance_policy=policy(1)), 'accepted')

    def test_regression_edits_best_eligible_image(self):
        pipe = self.make()
        self.to_images(pipe)
        _, result = self.candidate_review(pipe)
        pipe.accept(result)
        best_path = pipe.state['latest']['v1/c1']['path']
        self.wait(pipe)
        _, result = self.candidate_review(pipe, dict.fromkeys(IDS, 100), canon_pass=False)
        pipe.accept(result)
        request = self.wait(pipe)
        self.assertEqual(request['images'][0]['path'], best_path)
        self.assertEqual(request['images'][0]['role'], 'edit_target:best_eligible')

    def test_refinement_stop_is_not_successful_cli_exit(self):
        run = self.root/'refinement'
        write(run/'state.json', {'status':'finished','pending':None,
              'cards': {'card': {'status':'plateau','best':0,
                                'history':[{'quality':92,'eligible':True}]}}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = main(['refine-status','--run',str(run)])
        self.assertEqual(rc, 3)
        self.assertIn('stopped_unaccepted', out.getvalue())
        self.assertIn('"all_accepted": false', out.getvalue())

    def test_refinement_resume_returns_pending_review_not_new_generation(self):
        from setforge.refine import next_job
        run = self.root/'refinement'
        write(run/'state.json', {'pending':{'kind':'review','job_id':'card-edit-01',
              'request':'cards/card/iteration-01/request.json'}})
        request = next_job(run)
        self.assertEqual(request['kind'], 'review')
        self.assertEqual(request['job_id'], 'card-edit-01')

    def test_replanning_preserves_edit_counter_and_discards_old_edit_target(self):
        pipe = self.make()
        self.to_images(pipe)
        _, result = self.candidate_review(pipe)
        result.update(verdict='fail', action='replan')
        for item in result['criteria']:
            item['verdict'] = 'fail'
        pipe.accept(result)
        self.assertEqual(pipe.state['phase'], 'plan')
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'], 1)
        self.assertFalse(pipe.art_history('v1/c1')[0]['eligible'])
        self.assertIsNone(pipe.best_art('v1/c1'))
        self.wait(pipe)
        self.deliver(pipe)  # Replacement composition.
        self.wait(pipe)
        self.deliver(pipe)  # Composition review.
        request = self.wait(pipe)
        self.assertEqual(request['kind'], 'image')
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'], 2)
        self.assertFalse(any(x['role'].startswith('edit_target:') for x in request['images']))

    def test_failed_generator_calls_cannot_bypass_image_cap(self):
        with patch('setforge.engine.art_policy',return_value=policy(1)):
            pipe = self.make()
        self.to_images(pipe)
        for n in range(6):
            pipe.provider_failure(ValueError('TEST transport failure'))
            if n < 5:
                self.wait(pipe)
        calls = pipe.state['calls']
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertFalse(pipe.state['pending'])
        pipe.resume_attention('TEST general retry extension', {'max_attempts_per_task':100})
        with self.assertRaises(NeedsInput):
            pipe.advance()
        self.assertEqual(pipe.state['calls'], calls)
        self.assertFalse(pipe.state['accepted'])

    def test_new_policy_discards_exhausted_generator_attempts_and_continues_next_card(self):
        pipe=self.make();self.to_images(pipe)
        for n in range(4):
            pipe.provider_failure(ValueError('TEST recorded image transport failure'))
            if n<3:self.wait(pipe)
        request=self.wait(pipe)
        self.assertEqual(request['task_key'],'image:v1/c2')
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'],4)
        self.assertEqual(pipe.state['art_stops']['v1/c1'],'discarded')
        self.assertIn('v1/c1',pipe.state['discarded_cards'])
        self.assertIn('v1',pipe.state['discarded_sets'])
        self.assertNotIn('v1/c1',pipe.state['accepted'])

    def test_export_rechecks_numeric_acceptance(self):
        pipe = self.make()
        from fixture_worker import plan
        pipe.state['plan'] = plan()
        from setforge.allocation import check
        pipe.state['allocation_gate'] = check(pipe.state['plan'],pipe.state['allocation_policy'])
        for _, _, key in pipe.cards():
            pipe.state['accepted'][key] = {'scores':dict.fromkeys(IDS,90),'quality':90,
                                          'eligible':True,'art_iteration':0}
            pipe.state['art_stops'][key] = 'accepted'
        with self.assertRaisesRegex(ValueError, 'without scored acceptance'):
            export(self.run, pipe.state, pipe.config)
        self.assertFalse((self.run/'exports').exists())

    def test_historical_run_not_silently_upgraded(self):
        pipe = self.make()
        for k in ['art_acceptance','art_history','art_stops','art_invalidated','allocation_policy','allocation_gate','production_contract']:
            pipe.state.pop(k)
        pipe.save()
        old = Pipeline(self.run)
        self.assertFalse(old.scored_art)
        self.to_images(old)
        self.deliver(old)
        request = self.wait(old)
        self.assertNotIn('art_review', request['output_schema']['properties'])


if __name__ == '__main__':
    unittest.main()
