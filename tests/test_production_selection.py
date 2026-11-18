import unittest
from setforge.engine import NeedsInput
from setforge.io import read,write
import test_pipeline as legacy

class ProductionSelectionTests(unittest.TestCase):
    def setUp(self):
        legacy.PipelineTests.setUp(self)
        self.brief['production_mode']='pilot'
        self.config['rules']['select_before_art']=True
        write(self.project/'project.json',self.config)
    make=legacy.PipelineTests.make
    wait=legacy.PipelineTests.wait
    deliver=legacy.PipelineTests.deliver
    finish=legacy.PipelineTests.finish

    def test_selection_prevents_unrequested_art_and_exports_ten(self):
        pipe=self.make()
        with self.assertRaisesRegex(ValueError,'after'):
            pipe.choose_plan('v2','test choice','test')
        for _ in range(3):
            self.wait(pipe);self.deliver(pipe)
        with self.assertRaises(NeedsInput):pipe.advance()
        self.assertEqual(pipe.state['phase'],'plan_selection')
        self.assertEqual(pipe.state['calls'],3)
        self.assertIsNone(pipe.state['pending'])
        with self.assertRaisesRegex(ValueError,'Unknown'):pipe.choose_plan('missing','test choice','test')
        pipe.choose_plan('v2','Test one reviewed option','test-fixture')
        self.finish(pipe)
        self.assertEqual(len(pipe.state['accepted']),10)
        self.assertTrue(all(k.startswith('v2/') for k in pipe.state['accepted']))
        self.assertEqual(read(self.run/'export-check.json')['actual_cards'],10)
        self.assertIn('Состав без арта',(self.run/'report.html').read_text(encoding='utf-8'))

    def test_cross_variant_canon_source_is_not_silently_dropped(self):
        self.config['schema_version']=2
        write(self.project/'project.json',self.config)
        pipe=self.make()
        for _ in range(3):self.wait(pipe);self.deliver(pipe)
        with self.assertRaises(NeedsInput):pipe.advance()
        with self.assertRaisesRegex(ValueError,'canon source'):pipe.choose_plan('v2','Test invalid dependency','test')
        pipe.choose_plan('v1','Its canonical source is included','test')
        self.finish(pipe)
        self.assertEqual(len(pipe.state['accepted']),10)

if __name__=='__main__':unittest.main()
