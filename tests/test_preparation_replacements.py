"""Offline pre-art transport checks for replacement proposals and staging."""
import copy
from pathlib import Path
import tempfile
import unittest

from fixture_worker import plan
from setforge.allocation import policy as allocation_policy
from setforge.character_staging import policy as staging_policy
from setforge.io import write
from setforge.preparation import preflight_response
from setforge.schemas import production_plan


class ReplacementPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.plan = plan(2)
        for variant in self.plan['variants']:
            for card in variant['cards']:
                card['character_staging'] = []
        source = self.plan['variants'][0]['cards'][0]
        schema = production_plan()
        self.request = {
            'job_id': '0001-replacement_plan', 'kind': 'replacement_plan',
            'output_schema': copy.deepcopy(schema['properties']['variants']['items']['properties']['cards']['items']),
            'context': {'plan': self.plan, 'card': source, 'replacement_slot': 'v1/c1',
                        'rejected_candidates': [{'number': 1, 'card': copy.deepcopy(source)}],
                        'brief': {'variants': 3, 'wishes': ''},
                        'reference_catalog': [{'id': 'ref-001'}],
                        'collection_canon': [], 'production_contract': {'mode': 'full'},
                        'allocation_policy': allocation_policy({'rules': {}}, {}),
                        'character_staging_policy': staging_policy()},
        }
        # Historical schemas permit added staging fields only when explicitly
        # extended. Use the actual new-run card schema for this transport fixture.
        from setforge.character_staging import staging_schema
        self.request['output_schema']['properties']['character_staging'] = staging_schema()
        self.request['output_schema']['required'].append('character_staging')
        self.card = copy.deepcopy(source)
        self.card.update(title='Kite', subject='TEST new kite', scene='A kite with a curled tail.')
        self.card['concept']['canonical_subject'] = 'new-kite'
        write(self.run / 'state.json', {'read_only_marker': True})

    def check(self, card=None):
        write(self.run / 'jobs' / self.request['job_id'] / 'request.json', self.request)
        return preflight_response(self.run, self.request['job_id'], card or self.card)

    def test_new_card_is_checked_in_whole_plan_without_mutation(self):
        before = copy.deepcopy(self.request)
        state = (self.run / 'state.json').read_bytes()
        answer = self.check()
        self.assertEqual(answer['verdict'], 'pass', answer)
        self.assertEqual(self.request, before)
        self.assertEqual((self.run / 'state.json').read_bytes(), state)
        self.assertFalse((self.run / 'plans.json').exists())
        self.assertFalse((self.run / 'allocation.json').exists())
        self.assertIn('character_staging', {c['name'] for c in answer['checks']})

    def test_replacement_cannot_change_any_slot_allocation(self):
        for field, value in (('id', 'another'), ('background', 2), ('stars', 5),
                             ('golden', True), ('key_character_count', 1),
                             ('entity_ids', ['test-rover'])):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.card)
                changed[field] = value
                answer = self.check(changed)
                self.assertEqual(answer['verdict'], 'fail')
                self.assertEqual(answer['errors'][0]['check'], 'replacement_slot')
                self.assertIn(field, answer['errors'][0]['message'])

    def test_every_rejected_candidate_is_checked_for_actual_concept_change(self):
        second = copy.deepcopy(self.card)
        second.update(subject='Other TEST concept', scene='A different TEST action.')
        second['concept']['canonical_subject'] = 'other-test-concept'
        self.request['context']['rejected_candidates'].append({'number': 2, 'card': second})
        for field in ('canonical_subject', 'subject', 'scene'):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.card)
                if field == 'canonical_subject':
                    changed['concept'][field] = second['concept'][field]
                else:
                    changed[field] = ' ' + second[field].upper() + ' '
                answer = self.check(changed)
                self.assertEqual(answer['verdict'], 'fail')
                self.assertIn('genuinely new', answer['errors'][0]['message'])

    def test_new_card_cannot_duplicate_another_position(self):
        self.card['subject'] = self.plan['variants'][0]['cards'][1]['subject']
        answer = self.check()
        self.assertEqual(answer['verdict'], 'fail')
        self.assertIn('Identical card subjects', answer['errors'][0]['message'])

    def test_unknown_slot_and_unknown_reference_fail_before_art(self):
        self.request['context']['replacement_slot'] = 'v1/unknown'
        self.assertEqual(self.check()['errors'][0]['check'], 'replacement_slot')
        self.request['context']['replacement_slot'] = 'v1/c1'
        self.card['reference_ids'] = ['unknown']
        self.assertEqual(self.check()['errors'][0]['check'], 'plan_structure')

    def test_replacement_nameplate_preflight_checks_reconstructed_plan(self):
        self.request['context']['nameplate_policy'] = {'available_width': 3}
        answer = self.check()
        self.assertEqual(answer['verdict'], 'fail')
        self.assertIn('nameplate_policy', {e['check'] for e in answer['errors']})
        self.assertFalse((self.run / 'nameplate-check.json').exists())

    def test_replacement_staging_is_compared_with_the_other_nine_cards(self):
        staging = {'character_id': 'test-host', 'stance': 'standing', 'gesture': 'arms_spread',
                   'action': 'Welcomes the audience.', 'camera_azimuth': 'front', 'camera_elevation': 'eye_level'}
        for card in self.plan['variants'][0]['cards'][:2]:
            card.update(background=4, key_character_count=1, main_cast_scene=True,
                        character_staging=[copy.deepcopy(staging)])
        self.card.update(background=4, key_character_count=1, main_cast_scene=True,
                         character_staging=[copy.deepcopy(staging)])
        answer = self.check()
        self.assertEqual(answer['verdict'], 'fail')
        self.assertIn('character_staging', {e['check'] for e in answer['errors']})
        self.card['character_staging'][0].update(stance='kneeling', camera_azimuth='left_profile')
        answer = self.check()
        self.assertEqual(answer['verdict'], 'pass', answer)

    def test_regular_plan_also_requires_staging_when_policy_is_present(self):
        self.request.update(job_id='0001-plan', kind='plan', output_schema=production_plan())
        from setforge.character_staging import staging_schema
        self.request['output_schema']['properties']['variants']['items']['properties']['cards']['items']['properties']['character_staging'] = staging_schema()
        # This historical-shaped schema does not require staging itself, proving
        # that the optional immutable policy activates semantic preflight too.
        response = copy.deepcopy(self.plan)
        response['variants'][0]['cards'][0].pop('character_staging')
        answer = self.check(response)
        self.assertEqual(answer['verdict'], 'fail')
        self.assertIn('character_staging', {e['check'] for e in answer['errors']})


if __name__ == '__main__':
    unittest.main()
