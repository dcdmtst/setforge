"""Pre-art gate integration; only labelled offline fixtures are generated."""
import contextlib,copy,io,json,unittest
from setforge.cli import main
from setforge.engine import Pipeline
from setforge.io import read,write,sha
from fixture_worker import plan
import test_pipeline as fixtures

class NameplateIntegrationTests(unittest.TestCase):
    setUp=fixtures.PipelineTests.setUp
    make=fixtures.PipelineTests.make
    wait=fixtures.PipelineTests.wait
    deliver=fixtures.PipelineTests.deliver
    to_images=fixtures.PipelineTests.to_images

    def test_new_run_snapshots_nameplate_gate_before_first_image(self):
        pipe=self.make();self.assertIn('nameplate_policy',pipe.state)
        self.assertEqual(read(self.run/'inputs/nameplate-policy.json')['target_height'],18)
        req=self.to_images(pipe);self.assertEqual(req['kind'],'image')
        self.assertEqual(read(self.run/'nameplate-check.json')['verdict'],'pass')
        self.assertTrue(all(j['kind']!='image' for j in pipe.state['jobs'][:-1]))
        pipe.check_snapshot()

    def test_bad_name_blocks_before_art_and_cannot_be_fixed_by_shrinking(self):
        pipe=self.make();self.wait(pipe);self.deliver(pipe);self.wait(pipe)
        candidate=plan();candidate['variants'][0]['cards'][0]['title']='Strawberry Picnic Basket'
        original=copy.deepcopy(candidate)
        pipe.accept(candidate,'OFFLINE TEST FIXTURE','not-real-art')
        self.assertEqual(candidate,original)
        self.assertIsNone(pipe.state['plan'])
        self.assertFalse(any(j['kind']=='image' for j in pipe.state['jobs']))
        feedback=pipe.state['feedback']['plan:plan']
        from setforge.ui_skin import measure_label
        self.assertIn(str(measure_label('Strawberry Picnic Basket')['width']),feedback);self.assertIn('160',feedback)
        self.assertIn('replace the card concept',feedback)

    def test_existing_contract_without_gate_is_not_silently_upgraded(self):
        # Explicit offline historical-state fixture, never a production switch.
        pipe=self.make();pipe.state.pop('nameplate_policy');pipe.save()
        pipe=Pipeline(self.run);self.assertNotIn('nameplate_policy',pipe.state)
        pipe.validate_plan(plan())

    def test_new_run_cannot_disable_nameplate_gate(self):
        self.config['rules']['title_preflight']=False
        write(self.project/'project.json',self.config)
        with self.assertRaisesRegex(ValueError,'mandatory'):
            self.make()

    def test_cli_checks_candidate_plan_without_changing_titles(self):
        candidate=plan();candidate['variants'][0]['cards'][0]['title']='Strawberry Picnic Basket'
        file=self.root/'candidate.json';write(file,candidate);before=sha(file)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc=main(['nameplate-check','--plan',str(file)])
        result=json.loads(out.getvalue())
        self.assertEqual(rc,2);self.assertEqual(sha(file),before)
        entry=next(x for x in result['labels'] if x['key']=='v1/c1')
        self.assertEqual(entry['text'],'Strawberry Picnic Basket');from setforge.ui_skin import measure_label
        self.assertEqual(entry['measured_width'],measure_label(entry['text'])['width'])
