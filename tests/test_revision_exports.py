"""Synthetic revision/export boundary regressions; not production visual QA."""
import copy
from types import SimpleNamespace
import unittest

from PIL import Image
from fixture_worker import review
from setforge.delivery import result_manifest
from setforge.construction import extend_schema
from setforge.schemas import scored_card_review
from setforge.io import read,write,sha
from setforge.revision import revise_card
import test_public_distribution as distribution


class RevisionExportTests(unittest.TestCase):
    setUp=distribution.DistributionTests.setUp
    completed_fixture=distribution.DistributionTests.completed_fixture

    def exercise_revision(self,status,*,noncanonical_run=False):
        run=self.completed_fixture();state=read(run/'state.json');key='v2/c1'
        if status=='completed_with_rejections':
            state['accepted'].pop('v1/c10')
            state.update(discarded_sets={'v1':{'reason':'TEST three concepts exhausted'}},
                         reviewed_variants=['v2','v3'])
            write(run/'exports/manifest.json',[row for row in read(run/'exports/manifest.json') if row['key']!='v1/c10'])
        state['status']=status;state['orchestration']={'card_blockers':{}};state['feedback']={}
        previous=state['accepted'][key];previous['job_id']='initial-fixture'
        write(run/'state.json',state)
        original=read(run/'exports/manifest.json')
        retired=next(row for row in original if row['key']==key)
        retired_bytes=(run/retired['path']).read_bytes()
        others={row['key']:sha(run/row['path']) for row in original if row['key']!=key}
        # Exercise a real filesystem alias on every OS, including machines
        # whose temporary directory does not use a Windows 8.3 short name.
        pipeline_run=run/'..'/run.name if noncanonical_run else run
        pipe=SimpleNamespace(run=pipeline_run,state=state,quality_v2=True,
                             check_snapshot=lambda:None,find=lambda target:(None,{}),
                             save=lambda:write(run/'state.json',state))
        result=revise_card(pipe,key,'TEST explicit correction')
        current=read(run/'exports/manifest.json')
        self.assertEqual(current,[row for row in original if row['key']!=key])
        for row in current:self.assertEqual(sha(run/row['path']),others[row['key']])
        amendment=run/result['history']
        self.assertEqual(read(amendment/'exports/manifest.json'),original)
        request=read(amendment/'request.json')
        self.assertEqual((run/request['retired_export']['archived_path']).read_bytes(),retired_bytes)
        self.assertEqual(request['retired_export']['record'],retired)
        self.assertNotIn(key,state['accepted'])

        # An independently passed synthetic replacement native is now recorded.
        native=run/'jobs/revision-fixture/artifact.png';native.parent.mkdir(parents=True)
        Image.new('RGB',(430,480),'orange').save(native)
        artifact={**previous,'job_id':'revision-fixture','path':native.relative_to(run).as_posix(),'sha256':sha(native)}
        state['accepted'][key]=artifact;state['art_stops'][key]='accepted'
        response=review({'kind':'card_review','context':{'construction_artifact':artifact,
                         'card':{'background':artifact['observed_category']}},'required_criteria':['geometry'],
                         'output_schema':extend_schema(scored_card_review(2,allocated=True),'card_review')})
        write(run/'jobs/revision-review/response.json',response)
        state['art_history']={key:[{'artifact':copy.deepcopy(artifact),'review_job':'revision-review'}]}
        write(run/'state.json',state)
        manifest=result_manifest(run)
        self.assertFalse(manifest['full_delivery_ready'])
        self.assertEqual(manifest['accepted_cards'],len(state['accepted']))
        self.assertEqual(manifest['accepted_cards'],len(state['accepted']))
        exported=next(row for row in read(run/'exports/manifest.json') if row['key']==key)
        self.assertEqual(exported['source_sha256'],artifact['sha256'])
        self.assertNotEqual(exported['sha256'],retired['sha256'])
        self.assertTrue((run/exported['path']).is_file())
        for row in current:self.assertEqual(sha(run/row['path']),others[row['key']])

    def test_completed_order_retires_only_revised_export_binding(self):
        self.exercise_revision('complete')

    def test_partially_successful_order_rebuilds_only_corrected_derivative(self):
        self.exercise_revision('completed_with_rejections')

    def test_revision_accepts_a_noncanonical_run_path(self):
        self.exercise_revision('complete',noncanonical_run=True)


if __name__=='__main__':unittest.main()
