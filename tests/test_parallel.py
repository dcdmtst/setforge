"""Concurrent orchestration tests with labelled OFFLINE fixtures, never real art QA."""
import copy
import sqlite3
import unittest
import contextlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from setforge.acceptance import IDS
from setforge.engine import NeedsInput, Pipeline
from setforge.io import read, write
from fixture_worker import respond, review
import test_pipeline as legacy


class WorkQueueTests(unittest.TestCase):
    setUp = legacy.PipelineTests.setUp

    def queue(self, artists=2):
        from setforge.workqueue import WorkQueue
        self.run.mkdir()
        queue=WorkQueue(self.run);queue.configure(artists)
        return queue

    def register(self,queue,n,card=None,role='image',kind='image'):
        pending={'id':f'{n:04d}-{kind}','role':role,'task_key':kind+':'+(card or str(n)),
                 'request_sha256':str(n).zfill(64)}
        queue.register(pending,card=card,kind=kind)
        return pending

    def test_racing_claims_never_duplicate_or_exceed_configured_eight_artists(self):
        queue=self.queue(artists=8)
        for n in range(1,25):self.register(queue,n,card='set/card'+str(n))
        with ThreadPoolExecutor(max_workers=16) as workers:
            claims=list(workers.map(lambda n:queue.claim('worker-'+str(n),'image'),range(16)))
        claimed=[x for x in claims if x]
        self.assertEqual(len(claimed),8)
        self.assertEqual(len({x['id'] for x in claimed}),8)
        self.assertEqual(sum(x['status']=='leased' for x in queue.jobs()),8)

    def test_retry_prefers_its_previous_artist_without_holding_the_card_forever(self):
        queue=self.queue()
        self.register(queue,1,card='set/retry')
        self.register(queue,2,card='set/other')
        claim=queue.claim('artist-a','image')
        queue.deliver(claim['id'],'artist-a',claim['token'],{'note':'TEST fixture only'})
        queue.acknowledge(claim['id'])
        self.register(queue,3,card='set/retry')
        retry=queue.claim('artist-a','image')
        self.assertEqual(retry['id'],'0003-image')
        fallback=queue.claim('artist-b','image')
        self.assertEqual(fallback['id'],'0002-image')

    def test_request_identity_and_active_worker_claim_are_fenced(self):
        queue=self.queue()
        pending=self.register(queue,1,card='set/card')
        queue.register(copy.deepcopy(pending),card='set/card',kind='image')
        changed={**pending,'request_sha256':'changed'}
        with self.assertRaises(ValueError):queue.register(changed,card='set/card',kind='image')
        self.register(queue,2,card='set/other')
        claim=queue.claim('artist-a','image')
        self.assertIsNone(queue.claim('artist-a','image'))
        with self.assertRaises(ValueError):queue.heartbeat(claim['id'],'artist-b',claim['token'])
        self.assertEqual(queue.heartbeat(claim['id'],'artist-a',claim['token'])['token'],claim['token'])


class ParallelTests(unittest.TestCase):
    def setUp(self):
        legacy.PipelineTests.setUp(self)
        self.config['schema_version'] = 2
        write(self.project/'project.json', self.config)

    def make(self, artists=2):
        from setforge.parallel import enable
        from setforge.workqueue import WorkQueue
        pipe = enable(Pipeline.create(self.project, self.brief, self.run), artists=artists)
        self.queue = WorkQueue(self.run)
        return pipe

    def advance(self):
        from setforge.parallel import load
        pipe = load(self.run)
        try:
            pipe.advance()
        except NeedsInput:
            pass
        return load(self.run)

    def request(self, claim):
        return read(self.run/'jobs'/claim['id']/'request.json')

    def answer(self, claim, response=None, *, advance=True):
        req = self.request(claim)
        result = respond(req, self.run) if response is None else response
        self.queue.deliver(claim['id'], claim['worker'], claim['token'], result,
                           model='TEST FIXTURE — NOT A REAL MODEL')
        return self.advance() if advance else result

    def next_claim(self, roles=('reviewer','planner','image')):
        pipe = self.advance()
        for role in roles:
            claim = self.queue.claim('fixture-'+role, role)
            if claim:
                return claim
        return None

    def to_art(self, artists=2):
        self.make(artists)
        for _ in range(12):
            self.advance()
            claim = self.queue.claim('artist-a', 'image')
            if claim:
                return claim
            claim = self.next_claim(('reviewer','planner'))
            self.assertIsNotNone(claim, read(self.run/'state.json'))
            self.answer(claim)
        self.fail('Planning did not release any image job')

    def finish(self, review_transform=None, limit=220):
        for _ in range(limit):
            pipe = self.advance()
            if pipe.state['status'] == 'complete':
                return pipe
            claim = self.next_claim()
            if claim is None:
                return self.advance()
            req = self.request(claim)
            result = respond(req, self.run)
            if req['kind'] == 'card_review' and review_transform:
                review_transform(req, result)
            self.answer(claim, result)
        self.fail('Queue did not settle within fixture limit')

    def expire(self, claim):
        db=sqlite3.connect(self.run/'queue.sqlite3')
        try:
            db.execute('UPDATE jobs SET expires_at=0 WHERE id=?', (claim['id'],))
            db.commit()
        finally:
            db.close()

    def test_default_two_artists_and_configurable_larger_pool(self):
        first = self.to_art(artists=8)
        claims = [first]
        for n in range(1,8):
            claim = self.queue.claim('artist-'+str(n), 'image')
            self.assertIsNotNone(claim)
            claims.append(claim)
        self.assertEqual(len({x['id'] for x in claims}),8)
        self.assertIsNone(self.queue.claim('artist-overflow','image'))

    def test_two_artists_are_default_and_claims_do_not_mutate_main_state(self):
        from setforge.parallel import enable
        from setforge.workqueue import WorkQueue
        enable(Pipeline.create(self.project,self.brief,self.run))
        self.queue = WorkQueue(self.run)
        for _ in range(3):
            claim=self.next_claim(('reviewer','planner'))
            self.assertIsNotNone(claim)
            self.answer(claim)
        before=(self.run/'state.json').read_bytes()
        a=self.queue.claim('one','image'); b=self.queue.claim('two','image')
        self.assertIsNotNone(a);self.assertIsNotNone(b)
        self.assertIsNone(self.queue.claim('three','image'))
        self.assertEqual(before,(self.run/'state.json').read_bytes())

    def test_out_of_order_results_keep_card_and_review_job_identity(self):
        a=self.to_art(); b=self.queue.claim('artist-b','image')
        self.assertIsNotNone(b)
        reqa,reqb=self.request(a),self.request(b)
        self.assertNotEqual(reqa['task_key'],reqb['task_key'])
        # Deliver B before A; both are imported by the single coordinator.
        self.answer(b, advance=False);self.answer(a,advance=False)
        pipe=self.advance()
        for image,req in [(a,reqa),(b,reqb)]:
            key=req['task_key'].split(':',1)[1]
            self.assertEqual(pipe.state['latest'][key]['job_id'],image['id'])
            self.assertEqual(next(j for j in pipe.state['jobs'] if j['id']==image['id'])['status'],'received')
        reviewed=[]
        for _ in range(4):
            claim=self.next_claim(('reviewer',));self.assertIsNotNone(claim)
            req=self.request(claim)
            if req['kind']=='entity_review':
                # v2 may qualify a new source before the older sibling review.
                self.answer(claim)
                continue
            self.assertEqual(req['kind'],'card_review')
            key=req['task_key'].split(':',1)[1]
            self.assertIn(key,[reqa['task_key'].split(':',1)[1],reqb['task_key'].split(':',1)[1]])
            self.answer(claim); reviewed.append((key,claim['id']))
            if len(reviewed)==2:break
        self.assertEqual(len(reviewed),2)
        pipe=self.advance()
        for key,review_id in reviewed:
            self.assertEqual(pipe.state['art_history'][key][0]['review_job'],review_id)
            self.assertIn(key,pipe.state['accepted'])

    def test_parallel_candidates_cannot_reuse_another_unreviewed_image(self):
        a=self.to_art();b=self.queue.claim('artist-b','image')
        first=respond(self.request(a),self.run)
        key_a=self.request(a)['task_key'].split(':',1)[1]
        key_b=self.request(b)['task_key'].split(':',1)[1]
        self.answer(a,first,advance=False)
        self.answer(b,copy.deepcopy(first),advance=False)
        pipe=self.advance()
        self.assertIn(key_a,pipe.state['latest'])
        self.assertNotIn(key_b,pipe.state['latest'])
        self.assertEqual(next(x for x in self.queue.jobs() if x['id']==b['id'])['status'],'error')

    def test_canon_consumers_wait_for_independent_entity_qualification(self):
        # Fixture source is v1/c3; other variants' c3 consume the same collection canon.
        self.make(artists=8)
        saw_source=False
        for _ in range(100):
            pipe=self.advance()
            source_qualified=any(e['id']=='test-rover' and e.get('qualification')=='entity_only'
                                 for e in pipe.state['canon'])
            image_jobs=[x for x in self.queue.jobs() if x['kind']=='image']
            consumers=[x for x in image_jobs if x.get('card') in ('v2/c3','v3/c3')]
            if not source_qualified:
                self.assertFalse(consumers)
            elif consumers:
                self.assertTrue(pipe.state['canon'])
                req=read(self.run/'jobs'/consumers[0]['id']/'request.json')
                self.assertTrue(any(x['role'].startswith('canon:test-rover:') for x in req['images']))
                saw_source=True;break
            claim=self.next_claim();self.assertIsNotNone(claim)
            self.answer(claim)
        self.assertTrue(saw_source)

    def test_same_result_delivery_is_idempotent(self):
        a=self.to_art();result=self.answer(a,advance=False)
        self.queue.deliver(a['id'],a['worker'],a['token'],copy.deepcopy(result),model='TEST FIXTURE — NOT A REAL MODEL')
        pipe=self.advance()
        key=self.request(a)['task_key'].split(':',1)[1]
        image_calls=pipe.state['task_attempts']['image:'+key]
        self.queue.deliver(a['id'],a['worker'],a['token'],result,model='TEST FIXTURE — NOT A REAL MODEL')
        again=self.advance()
        self.assertEqual(again.state['task_attempts']['image:'+key],image_calls)
        self.assertEqual(sum(j['id']==a['id'] for j in again.state['jobs']),1)
        with self.assertRaises(ValueError):
            self.queue.deliver(a['id'],a['worker'],a['token'],{'image_path':'different.png','note':'TEST'})

    def test_expired_image_lease_is_not_redispatched_and_late_result_can_reconcile(self):
        a=self.to_art();key=self.request(a)['task_key'].split(':',1)[1]
        self.expire(a)
        other=self.queue.claim('replacement','image')
        self.assertTrue(other is None or other['id']!=a['id'])
        job=next(x for x in self.queue.jobs() if x['id']==a['id'])
        self.assertEqual(job['status'],'uncertain')
        self.advance()
        matching=[j for j in self.queue.jobs() if j['kind']=='image' and j.get('card')==key]
        self.assertEqual(len(matching),1)
        self.answer(a)
        self.assertEqual(next(x for x in self.queue.jobs() if x['id']==a['id'])['status'],'done')

    def test_expired_reviewer_fencing_rejects_old_token(self):
        a=self.to_art();self.answer(a)
        old=self.next_claim(('reviewer',));self.assertIsNotNone(old)
        before=read(self.run/'state.json')['calls']
        self.expire(old)
        self.advance()
        new=self.queue.claim('replacement-reviewer','reviewer')
        self.assertNotEqual(new['id'],old['id']);self.assertNotEqual(new['token'],old['token'])
        self.assertGreater(read(self.run/'state.json')['calls'],before)
        with self.assertRaises(ValueError):
            self.queue.deliver(old['id'],old['worker'],old['token'],review(self.request(old)))
        pipe=self.answer(new)
        key=self.request(new)['task_key'].split(':',1)[1]
        self.assertEqual(len(pipe.state['art_history'][key]),1)

    def test_expired_reasoning_cannot_reclaim_forever_past_attempt_limit(self):
        self.config['limits']['max_attempts_per_task']=1
        write(self.project/'project.json',self.config)
        a=self.to_art();self.answer(a)
        old=self.next_claim(('reviewer',));self.assertIsNotNone(old)
        key=self.request(old)['task_key'].split(':',1)[1]
        self.expire(old)
        for _ in range(3):pipe=self.advance()
        reviews=[j for j in self.queue.jobs() if j['kind']=='card_review' and j['card']==key]
        self.assertEqual(len(reviews),1)
        self.assertIn(next(j for j in pipe.state['jobs'] if j['id']==old['id'])['status'],('error','cancelled'))
        self.assertIn(key,pipe.state['orchestration']['card_blockers'])
        self.assertIsNone(self.queue.claim('retry-beyond-cap','reviewer'))
        self.assertNotIn(key,pipe.state['accepted'])

    def test_recovery_after_state_commit_before_inbox_ack_does_not_repeat_review(self):
        from setforge.workqueue import WorkQueue
        a=self.to_art();self.answer(a)
        claim=self.next_claim(('reviewer',));self.assertIsNotNone(claim)
        self.answer(claim,advance=False)
        original=WorkQueue.acknowledge
        class SimulatedCrash(BaseException):
            pass
        def crash(queue,job,status='done'):
            if job==claim['id']:
                raise SimulatedCrash('TEST crash after state commit before queue acknowledgement')
            return original(queue,job,status)
        with patch.object(WorkQueue,'acknowledge',crash):
            with self.assertRaises(SimulatedCrash):self.advance()
        key=self.request(claim)['task_key'].split(':',1)[1]
        committed=read(self.run/'state.json')
        self.assertEqual(len(committed['art_history'][key]),1)
        pipe=self.advance()
        self.assertEqual(len(pipe.state['art_history'][key]),1)
        self.assertEqual(len([e for e in pipe.state['canon'] if e.get('established_by')==key]),
                         len([e for e in committed['canon'] if e.get('established_by')==key]))

    def test_crash_after_issue_before_queue_registration_recovers_same_job(self):
        from setforge.workqueue import WorkQueue
        a=self.to_art();self.answer(a,advance=False)
        original=WorkQueue.register
        class SimulatedCrash(BaseException):
            pass
        def crash(queue,pending,card=None,kind=None,priority=0):
            if kind=='image':raise SimulatedCrash('TEST crash after persisted issue')
            return original(queue,pending,card,kind,priority=priority)
        with patch.object(WorkQueue,'register',crash):
            with self.assertRaises(SimulatedCrash):self.advance()
        committed=read(self.run/'state.json');pending=committed['pending']
        self.assertIsNotNone(pending)
        req=read(self.run/'jobs'/pending['id']/'request.json')
        self.assertEqual(req['kind'],'image')
        key=req['task_key'].split(':',1)[1]
        before=committed['task_attempts']['image:'+key]
        recovered=self.advance()
        self.assertEqual(sum(j['id']==pending['id'] for j in self.queue.jobs()),1)
        self.assertEqual(recovered.state['task_attempts']['image:'+key],before)

    def test_local_plateau_does_not_block_other_cards_or_count_as_acceptance(self):
        from setforge.acceptance import policy
        with patch('setforge.engine.art_policy', return_value=policy(1)):
            self.make()
        def low(req,result):
            if req['task_key']=='card_review:v1/c1':
                result['art_review'].update(scores=dict.fromkeys(IDS,90),
                    feedback='TEST concrete material correction',next_strategy='TEST change material strategy')
        pipe=self.finish(low)
        self.assertEqual(pipe.state['art_stops']['v1/c1'],'plateau')
        self.assertEqual(len(pipe.state['art_history']['v1/c1']),3)
        self.assertEqual(len(pipe.state['accepted']),29)
        self.assertNotIn('v1/c1',pipe.state['accepted'])
        self.assertNotEqual(pipe.state['status'],'complete')
        self.assertFalse((self.run/'exports/manifest.json').exists())
        self.assertFalse(any(j['kind']=='set_review' for j in pipe.state['jobs']))

    def test_local_sixth_candidate_stops_at_five_edits_without_blocking_others(self):
        from setforge.acceptance import policy
        with patch('setforge.engine.art_policy', return_value=policy(1)):
            self.make()
        def low(req,result):
            if req['task_key']=='card_review:v1/c1':
                iteration=len(req['context']['art_history']);scores=dict.fromkeys(IDS,85)
                for name in ['technical','style','materials','palette','narrative'][:iteration]:scores[name]=90
                result['art_review'].update(scores=scores,feedback='TEST explicit correction',
                                            next_strategy='TEST next targeted material fix')
        pipe=self.finish(low)
        self.assertEqual(pipe.state['art_stops']['v1/c1'],'iteration_limit')
        self.assertEqual(len(pipe.state['art_history']['v1/c1']),6)
        self.assertEqual(pipe.state['task_attempts']['image:v1/c1'],6)
        self.assertEqual(len(pipe.state['accepted']),29)
        self.assertNotEqual(pipe.state['status'],'complete')

    def test_global_call_budget_is_reserved_before_parallel_dispatch(self):
        self.config['limits']['max_calls']=5
        write(self.project/'project.json',self.config)
        a=self.to_art(artists=8)
        before=read(self.run/'state.json')
        self.assertLessEqual(before['calls'],5)
        for _ in range(3):self.advance()
        self.assertLessEqual(read(self.run/'state.json')['calls'],5)

    def test_global_cost_budget_is_reserved_before_parallel_dispatch(self):
        self.config['providers']['image']['estimated_cost']=0.25
        self.config['limits']['max_estimated_cost']=0.5
        write(self.project/'project.json',self.config)
        self.to_art(artists=8)
        pipe=self.advance()
        self.assertLessEqual(pipe.state['estimated_cost'],0.5)
        self.assertLessEqual(sum(x['kind']=='image' for x in self.queue.jobs()),2)

    def test_full_three_variant_delivery_waits_for_whole_set_review(self):
        self.make()
        saw_set_review=False
        for _ in range(180):
            pipe=self.advance()
            if pipe.state['status']=='complete':break
            claim=self.next_claim();self.assertIsNotNone(claim,pipe.state.get('reason'))
            req=self.request(claim)
            if req['kind']=='set_review':
                self.assertEqual(len(pipe.state['accepted']),30)
                self.assertFalse((self.run/'exports/manifest.json').exists())
                self.assertEqual(len(req['context']['plan']['variants']),3)
                saw_set_review=True
            self.answer(claim)
        pipe=self.advance()
        self.assertTrue(saw_set_review)
        self.assertEqual(pipe.state['status'],'complete')
        self.assertEqual(len(pipe.state['accepted']),30)
        self.assertEqual(len(list((self.run/'exports').rglob('*.png'))),30)
        self.assertEqual(read(self.run/'export-check.json')['verdict'],'pass')

    def test_set_review_correction_invalidates_only_named_card_and_reruns_global_gate(self):
        self.make()
        first_set=None
        for _ in range(180):
            pipe=self.advance();claim=self.next_claim();self.assertIsNotNone(claim)
            req=self.request(claim)
            if req['kind']=='set_review':first_set=claim;break
            self.answer(claim)
        self.assertIsNotNone(first_set)
        before=copy.deepcopy(pipe.state['accepted'])
        result=review(self.request(first_set))
        result.update(verdict='fail',action='revise',affected_cards=['v1/c1'])
        next(x for x in result['criteria'] if x['name']=='style_consistency')['verdict']='fail'
        result['findings']=[{'code':'test-local-style','criterion':'style_consistency','severity':'blocking',
                             'targets':['v1/c1'],'evidence':'TEST FIXTURE local visual mismatch',
                             'fix':'TEST FIXTURE revise only target card'}]
        pipe=self.answer(first_set,result)
        self.assertEqual(len(pipe.state['accepted']),29)
        self.assertIn(before['v1/c1']['job_id'],pipe.state['art_invalidated'])
        for key,value in before.items():
            if key!='v1/c1':self.assertEqual(pipe.state['accepted'][key],value)
        self.assertFalse((self.run/'exports/manifest.json').exists())
        redo=self.next_claim();self.assertEqual(self.request(redo)['kind'],'image')
        self.assertEqual(self.request(redo)['task_key'],'image:v1/c1')
        self.answer(redo)
        card_review=self.next_claim();self.assertEqual(self.request(card_review)['kind'],'card_review')
        self.answer(card_review)
        second_set=self.next_claim();self.assertEqual(self.request(second_set)['kind'],'set_review')
        self.assertNotEqual(first_set['id'],second_set['id'])
        self.assertFalse((self.run/'exports/manifest.json').exists())
        pipe=self.answer(second_set)
        self.assertEqual(pipe.state['status'],'complete')
        self.assertEqual(len(pipe.state['accepted']),30)

    def test_revising_canon_source_invalidates_consumers_and_reestablishes_version_before_art(self):
        self.make()
        first_set=None
        for _ in range(180):
            pipe=self.advance();claim=self.next_claim();self.assertIsNotNone(claim)
            if self.request(claim)['kind']=='set_review':first_set=claim;break
            self.answer(claim)
        self.assertIsNotNone(first_set)
        source='v1/c3';consumers={'v2/c3','v3/c3'}
        old_art={k:copy.deepcopy(pipe.state['accepted'][k]) for k in {source}|consumers}
        old_version=next(e['version'] for e in pipe.state['canon'] if e['id']=='test-rover')
        previous_jobs={j['id'] for j in pipe.state['jobs']}
        result=review(self.request(first_set))
        result.update(verdict='fail',action='revise',affected_cards=[source])
        next(x for x in result['criteria'] if x['name']=='canon')['verdict']='fail'
        result['findings']=[{'code':'test-source-identity','criterion':'canon','severity':'blocking',
                             'targets':[source],'evidence':'TEST FIXTURE canon-source mismatch',
                             'fix':'TEST FIXTURE replace source before consumers'}]
        pipe=self.answer(first_set,result)
        self.assertEqual(len(pipe.state['accepted']),27)
        self.assertTrue(all(k not in pipe.state['accepted'] for k in {source}|consumers))
        jobs=[j for j in self.queue.jobs() if j['id'] not in previous_jobs and j['kind']=='image']
        self.assertEqual({j['card'] for j in jobs},{source})
        candidate=self.next_claim(('image',));self.assertEqual(self.request(candidate)['task_key'],'image:'+source)
        self.answer(candidate)
        self.assertFalse(any(j['id'] not in previous_jobs and j.get('card') in consumers for j in self.queue.jobs()))
        source_review=self.next_claim(('reviewer',));self.answer(source_review)
        identity_review=self.next_claim(('reviewer',))
        self.assertEqual(self.request(identity_review)['kind'],'entity_review')
        self.answer(identity_review)
        pipe=self.advance()
        canon=next(e for e in pipe.state['canon'] if e['id']=='test-rover')
        self.assertEqual(canon['version'],old_version+1)
        self.assertEqual(canon['images'],[pipe.state['accepted'][source]['path']])
        for n in range(2):
            claim=self.queue.claim('updated-consumer-'+str(n),'image')
            self.assertIsNotNone(claim)
            req=self.request(claim)
            self.assertIn(req['task_key'].split(':',1)[1],consumers)
            self.assertTrue(any(i['role']=='canon:test-rover:v'+str(old_version+1) and i['path']==canon['images'][0] for i in req['images']))
            self.answer(claim)
        pipe=self.finish()
        self.assertEqual(pipe.state['status'],'complete')
        self.assertTrue(all(pipe.state['accepted'][k]['job_id']!=old_art[k]['job_id'] for k in {source}|consumers))

    def test_loading_legacy_run_does_not_enable_queue_or_rewrite_it(self):
        from setforge.parallel import load
        pipe=Pipeline.create(self.project,self.brief,self.run)
        before=(self.run/'state.json').read_bytes()
        loaded=load(self.run)
        self.assertIs(type(loaded),Pipeline)
        self.assertEqual(before,(self.run/'state.json').read_bytes())
        self.assertFalse((self.run/'queue.sqlite3').exists())

    def test_public_cli_workers_deliver_without_writing_state_until_coordinate(self):
        from setforge.cli import main
        def cli(*args):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):rc=main(list(args))
            return rc,json.loads(out.getvalue())
        rc,result=cli('run','--project',str(self.project),'--theme','TEST queued CLI',
                      '--run',str(self.run),'--artists','8')
        self.assertEqual(rc,3)
        self.assertEqual(read(self.run/'state.json')['orchestration']['artists'],8)
        before=(self.run/'state.json').read_bytes()
        rc,claim=cli('claim','--run',str(self.run),'--worker','cli-reviewer','--role','reviewer')
        self.assertEqual(rc,0);self.assertEqual(claim['status'],'leased')
        req=read(self.run/'jobs'/claim['id']/'request.json')
        response=self.root/'cli-response.json';write(response,respond(req,self.run))
        rc,result=cli('deliver','--run',str(self.run),'--job',claim['id'],
                      '--worker','cli-reviewer','--token',claim['token'],'--response',str(response))
        self.assertEqual(rc,0);self.assertEqual(result['status'],'submitted')
        self.assertEqual(before,(self.run/'state.json').read_bytes())
        rc,result=cli('coordinate','--run',str(self.run),'--artists','2')
        self.assertEqual(rc,3)
        state=read(self.run/'state.json')
        self.assertEqual(state['phase'],'plan')
        self.assertEqual(state['orchestration']['artists'],2)
        self.assertEqual(next(j for j in state['jobs'] if j['id']==claim['id'])['status'],'received')
