"""Offline provider-boundary regressions; no art or model calls."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from setforge.image_execution import image_call, start
from setforge.image_prompts import IMAGEGEN_PROMPT_LIMIT
from setforge.io import read, sha, write


class ImagePromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        write(self.run / 'inputs/project.json', {'providers': {'image': {'type': 'files', 'max_images': 5}}})
        (self.run / 'style.png').write_bytes(b'SYNTHETIC ORIGINAL IMAGE BYTES')
        (self.run / 'canon.png').write_bytes(b'SYNTHETIC CANON IMAGE BYTES')
        self.request = {
            'job_id': '0003-image', 'kind': 'image', 'task_key': 'image:set/card',
            'instruction': 'Render ONE card. Return image_path relative to run root (or an absolute local path), plus note. Preserve all toes.',
            'context': {
                'brief': {'theme': 'TEST circus', 'wishes': 'EXACT free-form wish: animals do not cook themselves'},
                'rules': {'output_size': [430, 480]},
                'style': 'Distinct satin timber, soft matte cloth, no oily sheen.',
                'card': {'id': 'card', 'subject': 'TEST rope', 'scene': 'Rope ties through metal eyes.',
                         'background': 4, 'materials': 'Dry rope', 'preserve': ['two platforms']},
                'variant': {'id': 'set', 'premise': 'Evening circus', 'entities': [{'id': 'UNRELATED ENTITY'}]},
                'canon': [{'id': 'canon', 'invariants': ['TWO ears'], 'allowed_changes': ['Pose'],
                           'approved_components': ['face'], 'excluded_components': ['old prop']}],
                'quality_guide': 'Check physical joins and distinct materials.',
                'art_history': [{'scores': 'HISTORY SCORE DATA' * 5000}],
                'art_acceptance': {'threshold': 93.5},
                'custom_constraints': ['Never omit this custom constraint'],
            },
            'feedback': {
                'summary': 'WHOLE SET SUMMARY' * 3000,
                'findings': [{'targets': ['set/card'], 'fix': 'Knot both ends through rings.', 'evidence': 'Rope floats'},
                             {'targets': ['other/card'], 'fix': 'UNRELATED FINDING' * 3000}],
                'construction_rechecks': [{'card': 'other/card', 'evidence': 'OTHER CONSTRUCTION' * 1000}],
                'art_review': {'scores': {'technical': 80}, 'feedback': 'Pull camera back.', 'next_strategy': 'Wider view.'}},
            'images': [{'path': 'canon.png', 'role': 'canon:canon:v1'},
                       {'path': 'canon.png', 'role': 'edit_target:best_eligible'},
                       {'path': 'style.png', 'role': 'original_style_reference:ref-1'}],
            'prompt': 'ORIGINAL FULL IMMUTABLE PROMPT' * 2000 + '\n\nPHYSICAL CONSTRUCTION\nTrace both rope joints.',
        }
        self.save()

    def save(self, request=None):
        value = request or self.request
        write(self.run / 'jobs' / value['job_id'] / 'request.json', value)

    def test_bounded_prompt_preserves_exact_requirements_not_full_review_history(self):
        path = self.run / 'jobs/0003-image/request.json'
        before = path.read_bytes()
        call = image_call(self.run, self.request)
        self.assertLess(len(call['prompt']), IMAGEGEN_PROMPT_LIMIT)
        for text in ('EXACT free-form wish', 'Rope ties through metal eyes.', 'Dry rope',
                     'two platforms', 'TWO ears', 'old prop', 'Never omit this custom constraint',
                     'Knot both ends through rings.', 'Pull camera back.', 'Trace both rope joints.',
                     'Preserve all toes.', 'canon:canon:v1', 'edit_target:best_eligible', 'original_style_reference:ref-1'):
            self.assertIn(text, call['prompt'])
        for text in ('HISTORY SCORE DATA', 'UNRELATED FINDING', 'WHOLE SET SUMMARY',
                     'OTHER CONSTRUCTION', 'UNRELATED ENTITY', '"scores"'):
            self.assertNotIn(text, call['prompt'])
        self.assertEqual(call['prompt'].count('Distinct satin timber'), 1)
        self.assertEqual(len(call['referenced_image_paths']), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(call['request_sha256'], sha(path))
        self.assertEqual(call['prompt_policy']['policy'], 'current-card-faithful-v1')

    def test_technical_retry_recovers_direction_from_exact_previous_candidate(self):
        prior = copy.deepcopy(self.request)
        prior['job_id'] = '0001-image'
        self.save(prior)
        self.request['feedback'] = 'HTTP400 prompt too long 32682 > 32000'
        self.save()
        call = image_call(self.run, self.request)
        self.assertIn('Knot both ends through rings.', call['prompt'])
        self.assertNotIn('HTTP400', call['prompt'])
        self.assertEqual(call['prompt_policy']['feedback_source_job'], '0001-image')

    def test_recorded_non_http_provider_error_also_recovers_art_feedback(self):
        prior = copy.deepcopy(self.request)
        prior['job_id'] = '0001-image'
        self.save(prior)
        write(self.run / 'jobs/0002-image/generation-record.json', {'status': 'failed', 'error': 'Unexpected tool disconnect'})
        self.request['feedback'] = 'Unexpected tool disconnect'
        call = image_call(self.run, self.request)
        self.assertIn('Knot both ends through rings.', call['prompt'])
        self.assertNotIn('Unexpected tool disconnect', call['prompt'])

    def test_feedback_is_never_recovered_across_replacement_candidate(self):
        prior = copy.deepcopy(self.request)
        prior['job_id'] = '0001-image'
        prior['context']['card_candidate'] = {'number': 1}
        self.save(prior)
        self.request['context']['card_candidate'] = {'number': 2}
        self.request['feedback'] = 'HTTP400 prompt too long'
        call = image_call(self.run, self.request)
        self.assertNotIn('Knot both ends through rings.', call['prompt'])
        self.assertIsNone(call['prompt_policy']['feedback_source_job'])
        self.request['context'].pop('card_candidate')
        prior['context'].pop('card_candidate')
        prior['context']['card']['subject'] = 'Different replacement'
        self.save(prior)
        self.assertNotIn('Knot both ends through rings.', image_call(self.run, self.request)['prompt'])

    def test_required_overflow_fails_without_truncation_or_timer(self):
        self.request['context']['brief']['wishes'] = 'REQUIRED WISH ' * 3000
        self.save()
        with patch('setforge.image_execution._owner'):
            with self.assertRaisesRegex(ValueError, 'No call/timer was started'):
                start(self.run, '0003-image', 'artist', '-LEASE', tool='imagegen')
        self.assertFalse((self.run / 'jobs/0003-image/generation-record.json').exists())
        self.assertFalse((self.run / 'jobs/0003-image/generation-inputs.json').exists())

    def test_custom_prompt_limit_is_checked_before_any_execution_artifacts(self):
        override = self.run / 'exact.txt'
        override.write_text('x' * (IMAGEGEN_PROMPT_LIMIT + 1), encoding='utf8')
        with patch('setforge.image_execution._owner'):
            with self.assertRaisesRegex(ValueError, '28000'):
                start(self.run, '0003-image', 'artist', '-LEASE', prompt_file=override)
        self.assertFalse((self.run / 'jobs/0003-image/generation-record.json').exists())
        self.assertFalse((self.run / 'jobs/0003-image/generation-prompt.txt').exists())

    def test_valid_custom_prompt_is_exact_even_when_generated_default_is_oversized(self):
        self.request['context']['brief']['wishes'] = 'ESSENTIAL ' * 4000
        self.save()
        override = self.run / 'exact.txt'
        exact = 'Worker faithful exact prompt, including input role instructions.'
        override.write_text(exact, encoding='utf8')
        with patch('setforge.image_execution._owner'):
            result = start(self.run, '0003-image', 'artist', '-LEASE', prompt_file=override)
        self.assertEqual(result['prompt'], exact)
        self.assertEqual((self.run / 'jobs/0003-image/generation-prompt.txt').read_text(encoding='utf8'), exact)
        self.assertEqual(read(self.run / 'jobs/0003-image/generation-inputs.json')['prompt_policy']['policy'], 'exact-worker-override')


if __name__ == '__main__':
    unittest.main()
