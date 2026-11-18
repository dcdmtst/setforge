"""Offline transport/cache regressions, never simulated production visual QA."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fixture_worker import plan, respond, review
from setforge.allocation import policy
from setforge.preparation import InspectedReferenceCache, framing_guidance, preflight_response
from setforge.schemas import ENTITY_REVIEW, REVIEW_V2, production_plan


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'run'
        self.run.mkdir()
        (self.run / 'state.json').write_text('{"immutable_test_marker":true}')

    def request(self, kind='set_review', **context):
        req = {'job_id': '0001-' + kind, 'kind': kind,
               'output_schema': copy.deepcopy(REVIEW_V2),
               'required_criteria': ['coherence', 'canon'],
               'context': {'plan': plan(2), **context}}
        folder = self.run / 'jobs' / req['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        self.save(req)
        return req

    def save(self, req):
        (self.run / 'jobs' / req['job_id'] / 'request.json').write_text(json.dumps(req), encoding='utf8')

    def check(self, req, answer):
        return preflight_response(self.run, req['job_id'], answer)

    def test_exact_request_schema_and_complete_read_only_preflight(self):
        req = self.request()
        before = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        result = self.check(req, review(req))
        self.assertEqual(result['verdict'], 'pass')
        self.assertTrue(result['structural_preflight_only'])
        self.assertEqual(before, {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob('*') if p.is_file()})
        req['output_schema']['properties']['summary'] = {'const': 'EXACT saved contract'}
        self.save(req)
        self.assertEqual(self.check(req, review(req))['errors'][0]['check'], 'output_schema')

    def test_variant_id_in_minor_finding_is_rejected_before_delivery(self):
        req = self.request()
        answer = review(req)
        answer['findings'] = [{'code': 'texture', 'criterion': 'coherence', 'severity': 'minor',
                               'targets': ['v1'], 'evidence': 'TEST texture observation', 'fix': 'TEST future broader cues'}]
        self.assertIn('unknown target', self.check(req, answer)['errors'][0]['message'])
        answer['findings'][0]['targets'] = ['v1/c1']
        self.assertEqual(self.check(req, answer)['verdict'], 'pass')

    def test_affected_cards_need_known_exact_keys_even_on_replan(self):
        req = self.request()
        answer = review(req)
        answer.update(verdict='uncertain', action='clarify', affected_cards=['v2'])
        answer['criteria'][0]['verdict'] = 'uncertain'
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')
        answer['affected_cards'] = ['v2/c2']
        self.assertEqual(self.check(req, answer)['verdict'], 'pass')

    def test_missing_unknown_duplicate_and_contradictory_criteria(self):
        req = self.request()
        for change in ('missing', 'unknown', 'duplicate', 'contradiction'):
            with self.subTest(change=change):
                answer = review(req)
                if change == 'missing': answer['criteria'].pop()
                elif change == 'unknown': answer['criteria'][0]['name'] = 'invented'
                elif change == 'duplicate': answer['criteria'][0]['name'] = 'canon'
                else: answer['verdict'] = 'fail'
                self.assertEqual(self.check(req, answer)['verdict'], 'fail')

    def test_failed_criterion_requires_blocker_and_exact_revision_targets(self):
        req = self.request()
        answer = review(req)
        answer.update(verdict='fail', action='revise', affected_cards=['v1/c1'])
        answer['criteria'][0]['verdict'] = 'fail'
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')
        answer['findings'] = [{'code': 'crop', 'criterion': 'coherence', 'severity': 'blocking',
                               'targets': ['v1/c2'], 'evidence': 'TEST clipped main silhouette', 'fix': 'TEST wider camera'}]
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')
        answer['affected_cards'] = ['v1/c2']
        self.assertEqual(self.check(req, answer)['verdict'], 'pass')
        answer['findings'][0]['fix'] = ' '
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')

    def test_passed_response_cannot_contain_blocker(self):
        req = self.request()
        answer = review(req)
        answer['findings'] = [{'code': 'bad', 'criterion': 'canon', 'severity': 'blocking',
                               'targets': ['v1/c1'], 'evidence': 'TEST mismatch', 'fix': 'TEST preserve identity'}]
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')

    def plan_request(self):
        req = self.request('plan', brief={'variants': 3, 'wishes': 'UNCHANGED TEST WISH'},
                           reference_catalog=[{'id': 'ref-001'}], production_contract={'mode': 'full'})
        req.pop('required_criteria')
        req['output_schema'] = production_plan()
        req['context']['allocation_policy'] = policy({'rules': {}}, {})
        self.save(req)
        return req

    def test_plan_preart_allocation_is_read_only_and_does_not_hide_cast_error(self):
        req = self.plan_request()
        answer = plan(2)
        before = copy.deepcopy(answer)
        self.assertEqual(self.check(req, answer)['verdict'], 'pass')
        self.assertEqual(answer, before)
        answer['variants'][0]['cards'][0]['key_character_count'] = 1
        result = self.check(req, answer)
        self.assertEqual(result['verdict'], 'fail')
        self.assertTrue(any(x['check'] == 'allocation_policy' for x in result['errors']))
        self.assertFalse((self.run / 'allocation.json').exists())

    def test_plan_nameplate_gate_uses_fixed_pixels_and_never_renames(self):
        req = self.plan_request()
        req['context']['nameplate_policy'] = {'available_width': 3}
        self.save(req)
        answer = plan(2)
        before = copy.deepcopy(answer)
        result = self.check(req, answer)
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('nameplate_policy', [x['check'] for x in result['errors']])
        self.assertEqual(answer, before)
        self.assertFalse((self.run / 'nameplate-check.json').exists())
        self.assertEqual(req['context']['brief']['wishes'], 'UNCHANGED TEST WISH')

    def test_historical_plan_missing_policies_is_explicitly_skipped(self):
        req = self.plan_request()
        req['context'].pop('allocation_policy')
        self.save(req)
        result = self.check(req, plan(2))
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual({x['name'] for x in result['checks'] if x['verdict'] == 'skipped'},
                         {'allocation_policy', 'nameplate_policy'})

    def test_plan_unknown_reference_entity_and_variant_count_fail_before_delivery(self):
        req = self.plan_request()
        for field in ('reference_ids', 'entity_ids'):
            with self.subTest(field=field):
                answer = plan(2)
                answer['variants'][0]['cards'][0][field] = ['unknown']
                self.assertEqual(self.check(req, answer)['verdict'], 'fail')
        req['context']['brief']['variants'] = 4
        self.save(req)
        self.assertEqual(self.check(req, plan(2))['verdict'], 'fail')

    def test_entity_exact_source_and_changed_content_are_checked(self):
        src = self.run / 'source.png'
        src.write_bytes(b'OFFLINE source bytes; not art')
        req = self.request('entity_review', entity={'id': 'sheep', 'source_card_id': 'v1/c1'},
                           source_artifact={'path': 'source.png', 'sha256': hashlib.sha256(src.read_bytes()).hexdigest()})
        req['output_schema'] = ENTITY_REVIEW
        self.save(req)
        answer = respond(req, self.run)
        self.assertEqual(self.check(req, answer)['verdict'], 'pass')
        answer['artifact_path'] = 'different.png'
        self.assertEqual(self.check(req, answer)['verdict'], 'fail')
        answer['artifact_path'] = 'source.png'
        src.write_bytes(b'CHANGED source')
        self.assertEqual(self.check(req, answer)['errors'][0]['check'], 'source_hash')

    def test_job_mismatch_and_path_escape_rejected(self):
        req = self.request()
        self.assertEqual(preflight_response(self.run, '../escape', review(req))['verdict'], 'fail')
        req['job_id'] = 'wrong'
        (self.run / 'jobs/0001-set_review/request.json').write_text(json.dumps(req))
        self.assertEqual(preflight_response(self.run, '0001-set_review', review(req))['verdict'], 'fail')

    def test_framing_guidance_is_generic_and_preserves_action_not_wishes(self):
        text = framing_guidance()
        for phrase in ('6–8%', 'skirt hem', 'chair', 'WHOLE group', 'shared action', 'Natural table/body occlusion', 'producer wishes'):
            self.assertIn(phrase, text)
        for phrase in ('ASSIGNED BACKGROUND', 'category 1', 'no support', 'categories 2/3', 'not an invented numeric gate'):
            self.assertIn(phrase, text)


class ReferenceInspectionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.source = self.run / 'reference.png'
        self.source.write_bytes(b'OFFLINE bytes, no actual visual claim')
        self.image = {'path': 'reference.png', 'role': 'original_style_reference:ref-001'}
        self.cache = InspectedReferenceCache(self.run / 'worker-cache.sqlite3', 'reviewer-one')

    def mark(self, image=None):
        record = self.cache.uninspected(self.run, [image or self.image])[0]
        return self.cache.mark_inspected(self.run, record, expected_sha256=record['sha256'], evidence='OFFLINE TEST attestation transport only')

    def test_reading_does_not_mark_or_create_cache(self):
        self.assertEqual(len(self.cache.uninspected(self.run, [self.image])), 1)
        self.assertEqual(len(self.cache.uninspected(self.run, [self.image])), 1)
        self.assertFalse(self.cache.path.exists())
        self.mark()
        self.assertEqual(self.cache.uninspected(self.run, [self.image]), [])

    def test_worker_role_and_canon_version_are_part_of_identity(self):
        self.mark()
        other = InspectedReferenceCache(self.cache.path, 'reviewer-two')
        self.assertEqual(len(other.uninspected(self.run, [self.image])), 1)
        for role in ('original_style_reference:ref-002', 'canon:sheep:v1'):
            self.assertEqual(len(self.cache.uninspected(self.run, [{**self.image, 'role': role}])), 1)
        self.mark({**self.image, 'role': 'canon:sheep:v1'})
        self.assertEqual(self.cache.uninspected(self.run, [{**self.image, 'role': 'canon:sheep:v1'}]), [])
        self.assertEqual(len(self.cache.uninspected(self.run, [{**self.image, 'role': 'canon:sheep:v2'}])), 1)

    def test_revised_content_invalidates_even_same_path_and_role(self):
        self.mark()
        self.source.write_bytes(b'NEW content')
        self.assertEqual(len(self.cache.uninspected(self.run, [self.image])), 1)

    def test_same_content_moved_path_can_reuse_exact_reference_role(self):
        self.mark()
        (self.run / 'copied.png').write_bytes(self.source.read_bytes())
        self.assertEqual(self.cache.uninspected(self.run, [{**self.image, 'path': 'copied.png'}]), [])

    def test_candidates_thumbnails_and_edit_targets_always_require_view(self):
        self.mark()
        for role in ('candidate_to_review', 'candidate_thumbnail_108x120', 'edit_target',
                     'identity_source_candidate', 'identity_source_thumbnail_108x120', 'candidate_set_contact_sheet'):
            image = {**self.image, 'role': role}
            with self.subTest(role=role):
                self.assertEqual(len(self.cache.uninspected(self.run, [image])), 1)
                with self.assertRaisesRegex(ValueError, 'cacheable'):
                    self.cache.mark_inspected(self.run, image, expected_sha256='a' * 64, evidence='TEST')

    def test_changed_after_view_and_false_manifest_hash_are_rejected(self):
        record = self.cache.uninspected(self.run, [self.image])[0]
        self.source.write_bytes(b'CHANGED after view')
        with self.assertRaises(ValueError):
            self.cache.mark_inspected(self.run, self.image, expected_sha256=record['sha256'], evidence='TEST')
        with self.assertRaises(ValueError):
            self.cache.uninspected(self.run, [{**self.image, 'sha256': 'a' * 64}])
        self.assertFalse(self.cache.path.exists())

    def test_mark_requires_explicit_valid_hash_and_nonblank_evidence(self):
        for digest, evidence in ((None, 'TEST'), ('a' * 64, ' ')):
            with self.assertRaises(ValueError):
                self.cache.mark_inspected(self.run, self.image, expected_sha256=digest, evidence=evidence)
        with self.assertRaises(ValueError):
            self.cache.uninspected(self.run, [{**self.image, 'path': '../outside.png'}])
        self.assertFalse(self.cache.path.exists())


if __name__ == '__main__':
    unittest.main()
