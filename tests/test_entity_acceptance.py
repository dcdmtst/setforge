"""Entity/card separation regressions using labelled OFFLINE fixtures, never art QA."""
import copy
import unittest
from unittest.mock import patch

from setforge.acceptance import IDS, policy
from setforge.io import read
from fixture_worker import respond
import test_parallel as parallel_fixtures


class EntityAcceptanceTests(unittest.TestCase):
    setUp = parallel_fixtures.ParallelTests.setUp
    make = parallel_fixtures.ParallelTests.make
    advance = parallel_fixtures.ParallelTests.advance
    request = parallel_fixtures.ParallelTests.request
    next_claim = parallel_fixtures.ParallelTests.next_claim

    def fixture_entity_review(self, request, verdict='pass'):
        """Explicit transport fixture, not a claim that test pixels depict an entity."""
        entity = request['context']['entity']
        artifact = request['context']['source_artifact']
        return {
            'entity_id': entity['id'], 'source_card_id': entity['source_card_id'],
            'artifact_path': artifact['path'], 'verdict': verdict,
            'full_inspected': True, 'thumbnail_inspected': True,
            'criteria': [{'name': name, 'verdict': verdict,
                          'evidence': 'TEST FIXTURE ONLY: explicit entity transport judgement'}
                         for name in ('identity', 'anatomy_geometry', 'materials', 'style', 'visibility')],
            'approved_components': ['TEST FIXTURE orange rover identity'] if verdict == 'pass' else [],
            'excluded_components': ['TEST FIXTURE rejected food prop; whole card remains unaccepted'],
            'feedback': 'TEST FIXTURE identity only qualified; rejected prop excluded' if verdict == 'pass' else 'TEST FIXTURE fix entity silhouette',
        }

    def answer(self, claim, response=None, *, advance=True):
        req = self.request(claim)
        result = (self.fixture_entity_review(req) if req['kind'] == 'entity_review'
                  else respond(req, self.run)) if response is None else response
        self.queue.deliver(claim['id'], claim['worker'], claim['token'], result,
                           model='TEST FIXTURE — NOT REAL ART OR VISUAL QA')
        return self.advance() if advance else result

    def reject_card(self, request, *, numeric_only=False):
        result = respond(request, self.run)
        result['art_review'].update(
            scores=dict.fromkeys(IDS, 90 if numeric_only else 95),
            feedback='TEST FIXTURE: replace rejected prop material',
            next_strategy='TEST FIXTURE: rebuild prop, retain separately qualified entity')
        if not numeric_only:
            key = request['task_key'].split(':', 1)[1]
            result.update(verdict='fail', action='revise', affected_cards=[key])
            next(c for c in result['criteria'] if c['name'] == 'materials')['verdict'] = 'fail'
            result['findings'] = [{'code': 'fixture-prop-material', 'criterion': 'materials',
                                  'severity': 'blocking', 'targets': [key],
                                  'evidence': 'TEST FIXTURE rejected prop, independent of entity identity',
                                  'fix': 'TEST FIXTURE rebuild prop material'}]
        return result

    def to_entity(self):
        self.make()
        for _ in range(100):
            claim = self.next_claim()
            self.assertIsNotNone(claim)
            req = self.request(claim)
            if req['kind'] == 'entity_review':
                self.assertEqual(req['context']['entity']['id'], 'test-rover')
                return claim, req
            response = self.reject_card(req) if req['kind'] == 'card_review' and req['task_key'] == 'card_review:v1/c3' else None
            self.answer(claim, response)
        self.fail('Entity source was never independently queued for review')

    def finish(self, reject_key=None, numeric_only=False, limit=240):
        for _ in range(limit):
            pipe = self.advance()
            claim = self.next_claim()
            if claim is None:
                return self.advance()
            req = self.request(claim)
            response = (self.reject_card(req, numeric_only=numeric_only)
                        if req['kind'] == 'card_review' and req['task_key'] == 'card_review:' + str(reject_key)
                        else None)
            self.answer(claim, response)
        self.fail('Offline queue did not settle')

    def test_qualified_entity_releases_consumers_without_accepting_rejected_source_card(self):
        claim, req = self.to_entity()
        source = copy.deepcopy(req['context']['source_artifact'])
        self.answer(claim)
        saw_consumer = False
        for _ in range(100):
            pipe = self.advance()
            canon = next(e for e in pipe.state['canon'] if e['id'] == 'test-rover')
            self.assertEqual(canon['qualification'], 'entity_only')
            self.assertEqual(canon['identity_review_job'], claim['id'])
            self.assertEqual(canon['identity_source_artifact'], source)
            self.assertEqual(canon['images'], [source['path']])
            self.assertTrue(canon['approved_components'])
            self.assertTrue(canon['excluded_components'])
            self.assertNotIn('v1/c3', pipe.state['accepted'])
            consumers = [j for j in self.queue.jobs() if j['kind'] == 'image' and j['card'] in ('v2/c3', 'v3/c3')]
            if consumers and pipe.state['art_history'].get('v1/c3'):
                consumer = read(self.run/'jobs'/consumers[0]['id']/'request.json')
                self.assertTrue(any(i['path'] == source['path'] and i['role'].startswith('canon:test-rover:') for i in consumer['images']))
                self.assertFalse(pipe.state['art_history']['v1/c3'][0]['eligible'])
                self.assertGreaterEqual(pipe.state['art_history']['v1/c3'][0]['quality'], 93.5)
                saw_consumer = True
                break
            job = self.next_claim()
            self.assertIsNotNone(job, {k: pipe.state.get(k) for k in ('status', 'phase', 'discarded_cards', 'discarded_sets', 'canon', 'reason')})
            request = self.request(job)
            self.answer(job, self.reject_card(request) if request['kind'] == 'card_review' and request['task_key'] == 'card_review:v1/c3' else None)
        self.assertTrue(saw_consumer)

    def test_failed_entity_review_never_freezes_or_releases_consumers(self):
        claim, req = self.to_entity()
        pipe = self.answer(claim, self.fixture_entity_review(req, 'fail'))
        self.assertFalse(any(e['id'] == 'test-rover' for e in pipe.state['canon']))
        self.assertFalse(any(j['kind'] == 'image' and j['card'] in ('v2/c3', 'v3/c3') for j in self.queue.jobs()))
        self.assertNotIn('v1/c3', pipe.state['accepted'])

    def test_entity_qualification_rejects_wrong_identity_source_or_incomplete_inspection(self):
        claim, req = self.to_entity()
        pipe = self.advance()
        for field, value in [('entity_id', 'unrelated'), ('source_card_id', 'v2/c3'),
                             ('artifact_path', 'jobs/forged/artifact.png'),
                             ('full_inspected', False), ('thumbnail_inspected', False)]:
            answer = self.fixture_entity_review(req); answer[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                pipe.qualify_entity(answer, req)
            self.assertFalse(any(e['id'] == 'test-rover' for e in pipe.state['canon']))
        answer = self.fixture_entity_review(req)
        answer['criteria'][0]['verdict'] = 'fail'
        with self.assertRaises(ValueError):
            pipe.qualify_entity(answer, req)

    def test_entity_freeze_is_idempotent_after_commit_before_inbox_acknowledgement(self):
        from setforge.workqueue import WorkQueue
        claim, req = self.to_entity()
        self.answer(claim, advance=False)
        with patch.object(WorkQueue, 'acknowledge', side_effect=RuntimeError('TEST crash before ack')):
            with self.assertRaisesRegex(RuntimeError, 'TEST crash'):
                self.advance()
        committed = read(self.run/'state.json')
        self.assertEqual(len([e for e in committed['canon'] if e['id'] == 'test-rover']), 1)
        pipe = self.advance()
        self.assertEqual(len([e for e in pipe.state['canon'] if e['id'] == 'test-rover']), 1)
        self.assertEqual(next(e for e in pipe.state['canon'] if e['id'] == 'test-rover')['identity_review_job'], claim['id'])
        self.assertEqual(next(j for j in self.queue.jobs() if j['id'] == claim['id'])['status'], 'done')

    def test_entity_review_cannot_qualify_an_artifact_changed_after_dispatch(self):
        claim, req = self.to_entity()
        artifact = self.run/req['context']['source_artifact']['path']
        artifact.write_bytes(b'TEST FIXTURE tampered source; not the reviewed PNG')
        pipe = self.advance()
        with self.assertRaises(ValueError):
            pipe.qualify_entity(self.fixture_entity_review(req), req)
        self.assertFalse(any(e['id'] == 'test-rover' for e in pipe.state['canon']))

    def test_later_rejected_source_edit_does_not_replace_frozen_entity_identity(self):
        claim, req = self.to_entity()
        source = copy.deepcopy(req['context']['source_artifact'])
        self.answer(claim)
        changed = False
        for _ in range(110):
            job = self.next_claim(); self.assertIsNotNone(job)
            request = self.request(job)
            response = (self.reject_card(request) if request['kind'] == 'card_review' and request['task_key'] == 'card_review:v1/c3' else None)
            pipe = self.answer(job, response)
            canon = next(e for e in pipe.state['canon'] if e['id'] == 'test-rover')
            self.assertEqual(canon['identity_source_artifact'], source)
            self.assertEqual(canon['images'], [source['path']])
            if request['kind'] == 'image' and request['task_key'] == 'image:v1/c3' and pipe.state['latest']['v1/c3']['path'] != source['path']:
                changed = True
                self.assertNotIn('v1/c3', pipe.state['accepted'])
                break
        self.assertTrue(changed)

    def test_new_policy_discards_after_three_edits_without_accepting_partial_sets(self):
        self.make()
        pipe = self.finish('v1/c1', numeric_only=True)
        self.assertEqual(pipe.state['art_acceptance']['version'], 2)
        self.assertEqual(pipe.state['art_acceptance']['max_edits'], 3)
        self.assertEqual(len(pipe.state['art_history']['v1/c1']), 4)
        self.assertEqual(pipe.state['art_stops']['v1/c1'], 'discarded')
        self.assertNotIn('v1/c1', pipe.state['accepted'])
        self.assertEqual(len(pipe.state['accepted']), 29)
        self.assertIn('v1', pipe.state['discarded_sets'])
        whole_set_jobs = [j for j in pipe.state['jobs'] if j['kind'] == 'set_review']
        self.assertTrue(whole_set_jobs, 'Fully accepted other sets still receive set review')
        for job in whole_set_jobs:
            reviewed = read(self.run/'jobs'/job['id']/'request.json')['context']['plan']['variants']
            self.assertEqual({v['id'] for v in reviewed}, {'v1', 'v2', 'v3'})
            self.assertEqual(read(self.run/'jobs'/job['id']/'request.json')['context']['delivery_variant_ids'],['v2','v3'])
            self.assertTrue(all(len(v['cards']) == 10 for v in reviewed))
        self.assertNotEqual(pipe.state['status'], 'complete')
        self.assertFalse(list((self.run/'exports').rglob('*.png')))

    def test_policy_amendment_preserves_two_existing_edits_and_releases_only_one_more(self):
        with patch('setforge.engine.art_policy', return_value=policy(1)):
            self.make()
        pipe = self.finish('v1/c1', numeric_only=True)
        self.assertEqual(pipe.state['art_stops']['v1/c1'], 'plateau')
        self.assertEqual(len(pipe.state['art_history']['v1/c1']), 3)
        snapshot = (self.run/'inputs/acceptance-policy.json').read_bytes()
        history = copy.deepcopy(pipe.state['art_history']['v1/c1'])
        image_attempts = pipe.state['task_attempts']['image:v1/c1']
        pipe.amend_policy('TEST explicit user-authorized change, preserving real history')
        pipe = self.finish('v1/c1', numeric_only=True)
        self.assertEqual((self.run/'inputs/acceptance-policy.json').read_bytes(), snapshot)
        self.assertEqual(pipe.state['art_history']['v1/c1'][:3], history)
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'], image_attempts + 1)
        self.assertEqual(len(pipe.state['art_history']['v1/c1']), 4)
        self.assertEqual(pipe.state['art_stops']['v1/c1'], 'discarded')
        self.assertNotIn('v1/c1', pipe.state['accepted'])

    def test_published_entity_preserves_exclusions_and_imports_portable_origin_provenance(self):
        import contextlib,io
        from setforge.cli import main
        from setforge.engine import Pipeline
        self.make();pipe=self.finish()
        self.assertEqual(pipe.state['status'],'complete')
        source=next(e for e in pipe.state['canon'] if e['id']=='test-rover')
        with contextlib.redirect_stdout(io.StringIO()):
            rc=main(['canon-publish','--project',str(self.project),'--run',str(self.run),'--entity','test-rover'])
        self.assertEqual(rc,0)
        published=read(self.project/'canon.json')[0]
        self.assertEqual(published['qualification'],'entity_only')
        self.assertEqual(published['approved_components'],source['approved_components'])
        self.assertEqual(published['excluded_components'],source['excluded_components'])
        self.assertEqual(published['identity_origin']['artifact'],source['identity_source_artifact'])
        followup=Pipeline.create(self.project,self.brief,self.root/'followup-run')
        followup.check_snapshot()
        self.assertTrue((followup.run/followup.state['canon'][0]['images'][0]).is_file())

    def test_whole_card_material_revision_keeps_independently_qualified_identity(self):
        from fixture_worker import review
        self.make()
        for _ in range(150):
            claim=self.next_claim();self.assertIsNotNone(claim)
            req=self.request(claim)
            if req['kind']=='set_review':break
            self.answer(claim)
        else:self.fail('No set review reached')
        pipe=self.advance();old=copy.deepcopy(pipe.state['canon'])
        consumers={key:copy.deepcopy(pipe.state['accepted'][key]) for key in ('v2/c3','v3/c3')}
        result=review(req);result.update(verdict='fail',action='revise',affected_cards=['v1/c3'])
        next(c for c in result['criteria'] if c['name']=='style_consistency')['verdict']='fail'
        result['findings']=[{'code':'fixture-food-material','criterion':'style_consistency','severity':'blocking',
                             'targets':['v1/c3'],'evidence':'TEST FIXTURE unrelated food material failed, entity identity remains good',
                             'fix':'TEST FIXTURE replace food surface only'}]
        pipe=self.answer(claim,result)
        self.assertEqual(pipe.state['canon'],old)
        self.assertNotIn('v1/c3',pipe.state['accepted'])
        self.assertTrue(all(pipe.state['accepted'][k]==a for k,a in consumers.items()))


if __name__ == '__main__':
    unittest.main()
