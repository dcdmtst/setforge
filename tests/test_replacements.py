"""Explicit offline lifecycle regressions. Fixture patterns are never visual QA."""
import copy
import unittest
from setforge.engine import Pipeline
from setforge.io import read
from setforge import replacements
from fixture_worker import respond
import test_entity_acceptance as entity
import test_parallel as parallel


class ReplacementTests(unittest.TestCase):
    setUp=parallel.ParallelTests.setUp
    def make(self, artists=2):
        from setforge.parallel import enable
        from setforge.workqueue import WorkQueue
        pipe=enable(Pipeline.create(self.project,self.brief,self.run),artists=artists)
        self.queue=WorkQueue(self.run)
        return pipe
    advance=parallel.ParallelTests.advance
    request=parallel.ParallelTests.request
    next_claim=parallel.ParallelTests.next_claim
    answer=entity.EntityAcceptanceTests.answer
    fixture_entity_review=entity.EntityAcceptanceTests.fixture_entity_review
    reject_card=entity.EntityAcceptanceTests.reject_card

    def finish_candidates(self, reject_through):
        self.make()
        for _ in range(260):
            pipe=self.advance()
            if pipe.state['status'] in ('complete','completed_with_rejections'):return pipe
            claim=self.next_claim()
            self.assertIsNotNone(claim, pipe.state['reason'])
            request=self.request(claim)
            response=None
            if request['kind']=='card_review' and request['task_key']=='card_review:v1/c1' and request['context'].get('card_candidate',1)<=reject_through:
                response=self.reject_card(request,numeric_only=True)
            self.answer(claim,response)
        self.fail('Replacement lifecycle did not terminate')

    def test_second_distinct_candidate_is_accepted_without_resetting_history_or_other_slots(self):
        pipe=self.finish_candidates(1)
        self.assertEqual(pipe.state['status'],'complete')
        self.assertEqual(len(pipe.state['accepted']),30)
        self.assertEqual(replacements.number(pipe.state,'v1/c1'),2)
        self.assertEqual(len(pipe.state['art_history']['v1/c1']),5)
        self.assertEqual([h['candidate'] for h in pipe.state['art_history']['v1/c1']],[1,1,1,1,2])
        self.assertEqual(len(pipe.art_history('v1/c1')),1)
        self.assertNotIn('v1',pipe.state.get('discarded_sets',{}))
        job=pipe.state['accepted']['v1/c1']['job_id']
        request=read(self.run/'jobs'/job/'request.json')
        self.assertFalse(any(i['role'].startswith('edit_target:') for i in request['images']))
        self.assertEqual(pipe.state['art_acceptance']['threshold'],93.5)
        self.assertTrue(any(j['kind']=='replacement_review' for j in pipe.state['jobs']))

    def test_only_third_distinct_exhausted_candidate_rejects_set(self):
        pipe=self.finish_candidates(3)
        self.assertEqual(pipe.state['status'],'completed_with_rejections')
        self.assertEqual(replacements.number(pipe.state,'v1/c1'),3)
        self.assertEqual(len(pipe.state['card_candidates']['v1/c1']['history']),3)
        self.assertEqual(len(pipe.state['art_history']['v1/c1']),12)
        self.assertEqual(len(pipe.state['accepted']),29)
        self.assertIn('v1',pipe.state['discarded_sets'])
        self.assertEqual(pipe.state['reviewed_variants'],['v2','v3'])
        last=next(j for j in reversed(pipe.state['jobs']) if j['kind']=='set_review')
        request=read(self.run/'jobs'/last['id']/'request.json')
        self.assertEqual(len(request['context']['plan']['variants']),3)
        self.assertEqual(set(request['context']['accepted_images']),set(pipe.state['accepted']))

    def test_provider_failure_preserves_targeted_art_direction(self):
        pipe=self.make();pipe.state['feedback']['image:v1/c1']={'fix':'retain the matte finish; repair the attachment'}
        pipe.state['pending']={'id':'test-image','role':'image','task_key':'image:v1/c1'}
        pipe.state['jobs'].append({'id':'test-image'})
        Pipeline.provider_failure(pipe,ValueError('HTTP400 prompt too long'))
        self.assertEqual(pipe.state['feedback']['image:v1/c1']['fix'],'retain the matte finish; repair the attachment')
        self.assertIn('HTTP400',pipe.state['execution_errors']['image:v1/c1'])

    def test_same_concept_cannot_be_renamed_to_buy_more_attempts(self):
        pipe=self.make()
        # A complete plan from the actual saved schema, using explicitly labelled offline fixtures.
        pipe.state['phase']='plan';request,_=Pipeline.prepare(pipe)
        plan=respond({**request,'job_id':'0001-plan'},self.run)
        pipe.state['plan']=plan
        key='v1/c1';old=copy.deepcopy(pipe.find(key)[1])
        replacements.exhausted(pipe,key,'OFFLINE initial candidate exhausted')
        pipe.state['replacement_current']=key
        proposal=copy.deepcopy(old);proposal['title']='New Name'
        with self.assertRaisesRegex(ValueError,'genuinely new'):
            replacements.proposed_plan(pipe,proposal)
