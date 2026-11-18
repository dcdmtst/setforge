"""Pre-art allocation checks; all generated test patterns and reviews are fixtures."""
import copy
import json
import unittest
from setforge.allocation import check, policy, require_gate
from setforge.engine import Pipeline
from setforge.io import write, read
from fixture_worker import plan, review
import test_pipeline as legacy


class AllocationTests(unittest.TestCase):
    setUp = legacy.PipelineTests.setUp
    make = legacy.PipelineTests.make
    wait = legacy.PipelineTests.wait
    deliver = legacy.PipelineTests.deliver
    to_images = legacy.PipelineTests.to_images
    finish = legacy.PipelineTests.finish

    def test_missing_stars_or_category_or_reason_blocks_before_art(self):
        pipe = self.make()
        for field in ['stars','background','rarity_reason','category_reason','golden','key_character_count']:
            p = plan()
            del p['variants'][0]['cards'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                pipe.validate_plan(p)
        self.assertEqual(pipe.state['calls'],0)
        self.assertFalse(pipe.state['latest'])

    def test_missing_category_and_bad_distribution_rejected(self):
        pipe = self.make()
        p = plan()
        for c in p['variants'][0]['cards']:
            c['background'] = 1
        with self.assertRaisesRegex(ValueError,'four background'):
            pipe.validate_plan(p)
        p = plan()
        p['variants'][0]['cards'][0]['stars'] = 5
        with self.assertRaisesRegex(ValueError,'Star distribution'):
            pipe.validate_plan(p)

    def test_rarity_independent_of_categories_and_five_stars_not_gold(self):
        pipe = self.make()
        p = plan()
        for v in p['variants']:
            for c,n in zip(v['cards'],[5,1,4,2,3,5,1,4,2,3]):
                c['stars'] = n
        pipe.validate_plan(p)
        self.assertFalse(any(c['golden'] for v in p['variants'] for c in v['cards']))

    def test_custom_star_distribution_is_saved_producer_input(self):
        self.brief['star_counts'] = [10,0,0,0,0]
        pipe = self.make()
        p = plan()
        for v in p['variants']:
            for c in v['cards']:
                c['stars'] = 1
        pipe.validate_plan(p)
        self.assertEqual(pipe.state['allocation_policy']['star_counts_source'],'producer')
        self.assertEqual(read(self.run/'inputs/allocation-policy.json')['star_counts'],[10,0,0,0,0])

    def test_generic_optional_policy_and_required_character_scene(self):
        self.config['rules']['exclude_main_cast_scenes'] = False
        write(self.project/'project.json',self.config)
        self.brief['characters'] = 'required'
        pipe = self.make()
        p = plan()
        with self.assertRaisesRegex(ValueError,'requires characters'):
            pipe.validate_plan(p)
        for v in p['variants']:
            c = v['cards'][-1]
            c.update(background=5,golden=True,main_cast_scene=True,key_character_count=2,
                     scene='TEST two key characters in a small emotional scene.')
            v['cast_reason'] = 'TEST required two-character scene, with original visual references.'
        pipe.validate_plan(p)
        p['variants'][0]['cards'][-1]['key_character_count'] = 0
        with self.assertRaisesRegex(ValueError,'one or two'):
            pipe.validate_plan(p)

    def test_character_exclusion_conflict_is_explicit(self):
        self.brief['characters'] = 'required'
        with self.assertRaisesRegex(ValueError,'conflict'):
            self.make()

    def test_optional_cast_absence_needs_deliberate_reason(self):
        self.config['rules']['exclude_main_cast_scenes'] = False
        write(self.project/'project.json',self.config)
        pipe = self.make()
        self.assertEqual(pipe.state['allocation_policy']['characters'],'optional')
        p = plan()
        p['variants'][0]['cast_reason'] = ' '
        with self.assertRaisesRegex(ValueError,'cast decision'):
            pipe.validate_plan(p)

    def test_preart_gate_is_recorded_before_first_image_job(self):
        pipe = self.make()
        request = self.to_images(pipe)
        self.assertEqual(request['kind'],'image')
        require_gate(pipe.state)
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        gate=next(i for i,e in enumerate(events) if e.get('gate')=='pre_art_allocation')
        image=next(i for i,e in enumerate(events) if e.get('event')=='job_issued' and e.get('kind')=='image')
        self.assertLess(gate,image)
        self.assertEqual(read(self.run/'allocation.json')['variants'][0]['star_counts'],[2,2,2,2,2])

    def test_stale_gate_cannot_generate_or_accept_pending_art(self):
        pipe = self.make()
        request = self.to_images(pipe)
        a,b=pipe.state['plan']['variants'][0]['cards'][:2]
        a['stars'],b['stars'] = 2,1
        with self.assertRaisesRegex(ValueError,'stale'):
            pipe.prepare()
        from fixture_worker import respond
        with self.assertRaisesRegex(ValueError,'stale'):
            pipe.accept(respond(request,self.run))
        self.assertFalse(pipe.state['latest'])

    def test_high_quality_cannot_accept_wrong_actual_category(self):
        pipe = self.make()
        self.to_images(pipe)
        self.deliver(pipe)
        request = self.wait(pipe)
        result = review(request)
        result['art_review'].update(observed_category=3,category_evidence='TEST wood instead of floating',
                                    feedback='TEST restore allocated category 1',next_strategy='TEST remove support')
        pipe.accept(result)
        self.assertFalse(pipe.state['accepted'])
        self.assertFalse(pipe.state['art_history']['v1/c1'][-1]['eligible'])
        self.assertEqual(pipe.state['phase'],'next_card')

    def test_category_observation_cannot_be_omitted(self):
        pipe = self.make()
        self.to_images(pipe)
        self.deliver(pipe)
        request = self.wait(pipe)
        result = review(request)
        del result['art_review']['observed_category']
        with self.assertRaisesRegex(ValueError,'observed_category'):
            pipe.accept(result)

    def test_refinement_cannot_start_or_generate_without_allocation(self):
        from setforge.refine import start,next_job
        run=self.root/'refine'
        with self.assertRaisesRegex(ValueError,'Assign and review'):
            start(run,self.root/'source',self.root/'audit')
        self.assertFalse(run.exists())
        write(run/'state.json',{'pending':None,'cards':{'lantern':{'status':'revise'}}})
        with self.assertRaisesRegex(ValueError,'no category/star allocation'):
            next_job(run)
        self.assertFalse((run/'cards').exists())

    def test_required_character_scene_survives_complete_file_pipeline(self):
        from setforge.engine import NeedsInput
        self.config['rules'].update(exclude_main_cast_scenes=False,select_before_art=True)
        write(self.project/'project.json',self.config)
        self.brief['characters']='required'
        self.brief['production_mode']='pilot'
        pipe=self.make()
        for _ in range(3):
            self.wait(pipe)
            self.deliver(pipe)
        with self.assertRaises(NeedsInput):
            pipe.advance()
        pipe.choose_plan('v1','TEST selection','test-fixture')
        self.finish(pipe)
        manifest=read(self.run/'exports/manifest.json')
        self.assertEqual(len(manifest),10)
        self.assertEqual(sum(c['golden'] for c in manifest),1)
        self.assertTrue({1,2,3,4,5}<={c['observed_category'] for c in manifest})
        self.assertEqual([sum(c['stars']==s for c in manifest) for s in range(1,6)],[2,2,2,2,2])

    def test_refinement_import_and_scored_review_use_allocated_category(self):
        from PIL import Image
        from setforge.refine import start,next_job,attach,review as refine_review
        from setforge.io import sha
        from setforge.acceptance import IDS
        source=self.root/'original'
        audit=self.root/'audit'
        raw=plan(allocated=False)
        allocated=plan()
        raw['variants'][0]['cards'][0]['id']='lantern'
        allocated['variants'][0]['cards'][0]['id']='lantern'
        write(source/'run/plans.json',raw)
        cards=raw['variants'][0]['cards']
        manifest=[]
        for n,c in enumerate(cards):
            image=source/f'original-{n}.png'
            Image.new('RGB',(430,480),(n*20,30,40)).save(image)
            manifest.append({'key':'v1/'+c['id'],'native':image.name,'native_sha256':sha(image),'observed_background':c['background']})
        write(source/'final/manifest.json',manifest)
        for n in [14,43,69,53,56,97,137]:
            path=source.parent/f'collection-project/references/CardC6_card_{n:02d}.png'
            path.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(430,480),'white').save(path)
        write(audit/'protocol.json',{})
        write(audit/'lunar-new-year-scores.json',[
            {'title':c['title'],'scores':dict.fromkeys(IDS,90),'blockers':[],
             'class_gate':'pass','evidence':'TEST baseline only'} for c in cards])
        write(audit/'lny-observations.json',[{'fix':'TEST fix'} for c in cards])
        matrix=check(allocated,policy(self.config,self.brief))
        write(self.root/'allocation.json',matrix)
        run=self.root/'refine'
        start(run,source,audit,self.root/'allocation.json')
        job=next_job(run)
        self.assertEqual(job['brief']['stars'],1)
        image=self.root/'edited.png'
        Image.new('RGB',(430,480),'red').save(image)
        request=attach(run,job['job_id'],image,1)
        self.assertIn('observed_category',request['required_review_fields'])
        answer={'scores':dict.fromkeys(IDS,95),'evidence':dict.fromkeys(IDS,'TEST evidence'),
                'blockers':[],'class_pass':True,'canon_pass':True,'full_inspected':True,
                'thumbnail_inspected':True,'summary':'TEST pass','feedback':'','next_strategy':'',
                'observed_category':1,'category_evidence':'TEST category evidence'}
        write(self.root/'review.json',answer)
        result=refine_review(run,job['job_id'],self.root/'review.json')
        self.assertEqual(result['cards']['lantern']['status'],'accepted')


if __name__=='__main__':
    unittest.main()
