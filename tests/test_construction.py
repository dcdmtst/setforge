"""Offline contract regression; synthetic fixtures never certify real art."""
import copy
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from setforge.construction import validate_card, validate_set, extend_schema
from setforge.io import sha, write
from setforge.schemas import scored_card_review, validate
from setforge.acceptance import score
from fixture_worker import review


class ConstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        Image.new('RGB', (430, 480), 'cyan').save(self.run/'test-fixture.png')
        self.artifact = {'path': 'test-fixture.png', 'sha256': sha(self.run/'test-fixture.png')}
        self.key = 'test/card'
        self.request = {'kind': 'card_review', 'context': {'construction_artifact': self.artifact,
            'card': {'background': 4}}, 'required_criteria': ['geometry'],
            'output_schema': extend_schema(scored_card_review(2, allocated=True), 'card_review')}
        self.response = review(self.request)

    def test_missing_audit_or_stale_pixels_cannot_pass(self):
        missing = copy.deepcopy(self.response);missing.pop('construction_review')
        with self.assertRaisesRegex(ValueError, 'Missing mandatory'):
            validate_card(self.run, missing, self.artifact, self.key)
        stale = copy.deepcopy(self.response);stale['construction_review']['artifact_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'exact candidate'):
            validate_card(self.run, stale, self.artifact, self.key)

    def test_high_score_does_not_override_bad_mechanism(self):
        response = self.response
        self.assertGreaterEqual(score(response['art_review']['scores']), 93.5)
        audit = response['construction_review'];audit['verdict'] = 'fail'
        audit['risk'] = 'interactive'
        mechanism = audit['assemblies'][0]['mechanism']
        mechanism.update(applicability='applicable', verdict='fail',
                         closed_position_or_travel='TEST: lid footprint does not match opening',
                         evidence='TEST: lid and opening boundaries contradict each other')
        with self.assertRaisesRegex(ValueError, 'regardless of Q95'):
            validate_card(self.run, response, self.artifact, self.key)
        response.update(verdict='fail', action='revise', affected_cards=[self.key])
        response['criteria'][0]['verdict'] = 'fail'
        response['findings'] = [{'criterion': 'geometry', 'severity': 'blocking', 'targets': [self.key]}]
        self.assertFalse(validate_card(self.run, response, self.artifact, self.key))

    def test_uncertain_mechanism_requires_revision_not_overall_pass(self):
        audit = self.response['construction_review'];audit['verdict'] = 'uncertain'
        audit['risk'] = 'interactive'
        audit['assemblies'][0]['mechanism'].update(applicability='applicable', verdict='uncertain')
        with self.assertRaisesRegex(ValueError, 'regardless of Q95'):
            validate_card(self.run, self.response, self.artifact, self.key)

    def test_whole_single_piece_can_explain_no_mechanism(self):
        validate(self.response, self.request['output_schema'])
        self.assertTrue(validate_card(self.run, self.response, self.artifact, self.key))

    def test_moving_parts_cannot_skip_final_recheck_as_single_piece(self):
        self.response['construction_review']['assemblies'][0]['mechanism']['applicability'] = 'applicable'
        with self.assertRaisesRegex(ValueError, 'cannot bypass'):
            validate_card(self.run, self.response, self.artifact, self.key)

    def test_final_set_cannot_skip_target_or_pass_failed_recheck(self):
        response = {'verdict': 'pass', 'action': 'none', 'affected_cards': [], 'findings': [],
                    'construction_rechecks': []}
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            validate_set(self.run, response, {self.key: self.artifact})
        response['construction_rechecks'] = [{'card': self.key, 'artifact_path': self.artifact['path'],
            'artifact_sha256': self.artifact['sha256'], 'verdict': 'fail', 'evidence': 'TEST: unsupported box'}]
        with self.assertRaisesRegex(ValueError, 'must block'):
            validate_set(self.run, response, {self.key: self.artifact})


class ConstructionPipelineTests(unittest.TestCase):
    import test_pipeline as legacy
    def setUp(self):
        self.legacy.PipelineTests.setUp(self)
        self.config['schema_version'] = 2
        write(self.project/'project.json', self.config)
    make = legacy.PipelineTests.make
    wait = legacy.PipelineTests.wait
    deliver = legacy.PipelineTests.deliver
    to_images = legacy.PipelineTests.to_images

    def test_real_accept_path_rejects_high_scoring_bad_assembly(self):
        pipe = self.make();self.to_images(pipe)
        self.deliver(pipe)
        request = self.wait(pipe)
        result = review(request)
        key = pipe.state['current']
        result['construction_review']['verdict'] = 'fail'
        result['construction_review']['risk'] = 'interactive'
        result['construction_review']['assemblies'][0]['mechanism'].update(
            applicability='applicable', verdict='fail',
            closed_position_or_travel='TEST lid does not cover its opening',
            evidence='TEST contradictory lid/opening boundaries')
        result.update(verdict='fail', action='revise', affected_cards=[key])
        next(c for c in result['criteria'] if c['name']=='geometry')['verdict'] = 'fail'
        result['findings'] = [{'code':'bad_closure', 'criterion':'geometry', 'severity':'blocking',
            'targets':[key], 'evidence':'TEST unmatched opening', 'fix':'TEST match lid and opening'}]
        result['art_review'].update(feedback='TEST fix the assembly', next_strategy='TEST simplify mechanism')
        self.assertGreaterEqual(score(result['art_review']['scores']),93.5)
        pipe.accept(result,'test-reviewer','not-a-real-model')
        self.assertNotIn(key,pipe.state['accepted'])
        self.assertFalse(pipe.state['art_history'][key][-1]['eligible'])
        self.assertEqual(pipe.state['art_stops'][key],'revise')


if __name__ == '__main__':unittest.main()
