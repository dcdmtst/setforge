"""Dependency scheduling with labelled offline fixtures, never model/art calls."""
import copy
import unittest
from unittest.mock import patch

from fixture_worker import plan, respond
from setforge.engine import Pipeline, NeedsInput
from setforge.io import read, write
from setforge.parallel import enable, load
from setforge.workqueue import WorkQueue
import test_parallel as parallel_fixtures


class SchedulingV2Tests(unittest.TestCase):
    setUp=parallel_fixtures.ParallelTests.setUp
    advance=parallel_fixtures.ParallelTests.advance
    request=parallel_fixtures.ParallelTests.request
    answer=parallel_fixtures.ParallelTests.answer

    def start(self,data=None,artists=2,reviewers=2,workers=None,legacy=False):
        pipe=Pipeline.create(self.project,self.brief,self.run)
        if legacy:
            pipe.state['orchestration']={'version':1,'artists':artists,'reviewers':1,'card_blockers':{}}
            pipe.save()
            self.queue=WorkQueue(self.run)
            self.queue.configure(artists=artists,managed_retries=True)
        else:
            enable(pipe,artists=artists,reviewers=reviewers,workers=workers)
            self.queue=WorkQueue(self.run)
        for _ in range(12):
            self.advance()
            images=[j for j in self.queue.jobs() if j['kind']=='image']
            if images:return load(self.run)
            claim=self.queue.claim('fixture-chief','planner') or self.queue.claim('fixture-chief','reviewer')
            self.assertIsNotNone(claim)
            req=self.request(claim)
            answer=copy.deepcopy(data) if data is not None and req['kind']=='plan' else respond(req,self.run)
            if req['kind']=='plan' and req['context'].get('character_staging_policy'):
                for v in answer['variants']:
                    for c in v['cards']:c.setdefault('character_staging',[])
            self.answer(claim,answer)
        self.fail('Planning did not reach image scheduling')

    def test_new_sources_late_in_plan_are_all_issued_before_ordinary_props(self):
        data=plan(2)
        for n,variant in enumerate(data['variants']):
            entity={'id':f'family-{n}','scope':'collection','source_card_id':f"{variant['id']}/c8",
                    'description':'OFFLINE FIXTURE identity','invariants':['TEST identity'],
                    'allowed_changes':['TEST pose']}
            variant['entities']=[entity]
            for card in variant['cards']:card['entity_ids']=[]
            variant['cards'][7]['entity_ids']=[entity['id']]
            variant['cards'][9]['entity_ids']=[entity['id']]
        self.start(data)
        source_keys={'v1/c8','v2/c8','v3/c8'}
        initial=[j for j in self.queue.jobs() if j['kind']=='image']
        self.assertEqual(len(initial),2)
        self.assertTrue({j['card'] for j in initial}<=source_keys)
        claim=self.queue.claim('fixture-artist','image')
        self.answer(claim)
        images=[j for j in self.queue.jobs() if j['kind']=='image']
        self.assertEqual({j['card'] for j in images[:3]},source_keys)
        self.assertTrue(all(j['priority']>=300 for j in images[:3]))

    def test_source_card_and_identity_review_overtake_older_ordinary_review(self):
        self.start()
        source=self.queue.claim('artist-source','image')
        self.assertEqual(source['card'],'v1/c3')
        prop=self.queue.claim('artist-prop','image')
        self.assertEqual(prop['card'],'v1/c1')
        self.answer(prop)
        ordinary=next(j for j in self.queue.jobs() if j['kind']=='card_review' and j['card']==prop['card'])
        self.answer(source)
        source_review=next(j for j in self.queue.jobs() if j['kind']=='card_review' and j['card']==source['card'])
        self.assertGreater(source_review['sequence'],ordinary['sequence'])
        self.assertGreater(source_review['priority'],ordinary['priority'])
        claim=self.queue.claim('independent-reviewer','reviewer')
        self.assertEqual(claim['id'],source_review['id'])
        self.answer(claim)
        identity=self.queue.claim('independent-reviewer','reviewer')
        self.assertEqual(identity['kind'],'entity_review')
        self.assertEqual(identity['card'],'v1/c3')
        pipe=self.answer(identity)
        self.assertTrue(any(e['id']=='test-rover' for e in pipe.state['canon']))
        # Free one occupied image reservation. A newly eligible consumer must
        # overtake the older unclaimed prop reservation in the persistent queue.
        older_prop=self.queue.claim('another-artist','image')
        self.assertIsNotNone(older_prop)
        self.answer(older_prop)
        consumer=self.queue.claim('fresh-artist','image')
        self.assertIn(consumer['card'],('v2/c3','v3/c3'))
        self.assertTrue(any(i['role'].startswith('canon:test-rover:') for i in self.request(consumer)['images']))

    def test_late_source_review_is_not_starved_by_a_full_ordinary_backlog(self):
        self.start()
        source=self.queue.claim('slow-source-artist','image')
        self.assertEqual(source['card'],'v1/c3')
        completed_props=[]
        for _ in range(10):
            prop=self.queue.claim('fast-prop-artist','image')
            if not prop:break
            completed_props.append(prop['card']);self.answer(prop)
        else:self.fail('Deferred candidates must still exert bounded review backpressure')
        self.assertGreaterEqual(len(completed_props),3)
        before=[j for j in self.queue.jobs() if j['kind']=='card_review' and j['status']=='ready']
        self.assertEqual(len(before),3)  # Four total slots, one useful source reserve.
        self.answer(source)
        reviews=[j for j in self.queue.jobs() if j['kind'] in ('card_review','entity_review') and j['status']=='ready']
        self.assertEqual(len(reviews),4)
        critical=self.queue.claim('independent-reviewer','reviewer')
        self.assertEqual((critical['kind'],critical['card']),('card_review','v1/c3'))
        self.assertTrue(all(critical['sequence']>job['sequence'] for job in before))
        self.answer(critical)
        identity=self.queue.claim('independent-reviewer','reviewer')
        self.assertEqual((identity['kind'],identity['card']),('entity_review','v1/c3'))
        pipe=self.answer(identity)
        self.assertTrue(any(e['id']=='test-rover' for e in pipe.state['canon']))
        self.assertTrue(any(j['kind']=='card_review' and j['card'] in completed_props
                            and j['status']=='ready' for j in self.queue.jobs()))

    def test_priority_refresh_changes_only_queue_metadata_as_canon_is_qualified(self):
        pipe=self.start()
        source=next(j for j in self.queue.jobs() if j['card']=='v1/c3')
        request=self.run/'jobs'/source['id']/'request.json';before=request.read_bytes()
        self.assertGreater(source['priority'],0)
        entity=copy.deepcopy(pipe.state['plan']['variants'][0]['entities'][0])
        entity.update(version=1,images=['TEST fixture source'])
        pipe.state['canon'].append(entity)
        with patch.object(pipe.queue,'set_priority',wraps=pipe.queue.set_priority) as update:
            pipe._refresh_priorities(pipe._scheduling_context())
            self.assertEqual(update.call_count,1)
            pipe._refresh_priorities(pipe._scheduling_context())
            self.assertEqual(update.call_count,1)
        self.assertEqual(request.read_bytes(),before)
        self.assertEqual(next(j for j in self.queue.jobs() if j['id']==source['id'])['priority'],0)

    def test_review_backlog_bound_grows_with_reviewer_capacity(self):
        pipe=self.start(reviewers=1)
        def fill_images():
            for _ in range(15):
                claim=self.queue.claim('fixture-images','image')
                if not claim:return
                self.answer(claim)
            self.fail('Image production did not respect the review backlog bound')
        fill_images()
        active=lambda:[j for j in self.queue.jobs() if j['status'] in ('ready','leased','submitted','uncertain')]
        self.assertEqual(sum(j['kind'] in ('card_review','entity_review') for j in active()),3)
        self.assertFalse(any(j['kind']=='image' for j in active()))
        pipe=load(self.run);pipe.set_capacity(reviewers=4);self.advance()
        fill_images()
        self.assertEqual(sum(j['kind'] in ('card_review','entity_review') for j in active()),6)

    def test_default_physical_capacity_and_explicit_capacity_remain_configurable(self):
        pipe=self.start(artists=2)
        self.assertEqual(pipe.state['orchestration']['version'],2)
        self.assertEqual(self.queue.configuration(),{'artists':2,'reviewers':2,'workers':3,'managed_retries':True})
        pipe.set_artists(8)
        self.assertEqual(self.queue.configuration()['workers'],9)
        pipe.set_capacity(workers=10,reviewers=5)
        pipe.set_artists(6)
        self.assertEqual(self.queue.configuration(),{'artists':6,'reviewers':5,'workers':10,'managed_retries':True})

    def test_historical_v1_keeps_plan_order_and_single_reviewer_configuration(self):
        pipe=self.start(legacy=True)
        images=[j for j in self.queue.jobs() if j['kind']=='image']
        self.assertEqual([j['card'] for j in images],['v1/c1','v1/c2'])
        self.assertTrue(all(j.get('priority',0)==0 for j in images))
        self.assertEqual(self.queue.configuration(),{'artists':2,'managed_retries':True})
        pipe.set_artists(4)
        self.assertEqual(pipe.state['orchestration']['version'],1)
        self.assertEqual(self.queue.configuration(),{'artists':4,'managed_retries':True})


if __name__=='__main__':unittest.main()
