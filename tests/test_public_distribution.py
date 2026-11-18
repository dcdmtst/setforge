"""Distribution contracts. Synthetic test images are not production art evidence."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
import zipfile

from PIL import Image,ImageDraw
from setforge.acceptance import IDS
from setforge.cli import main
from setforge.delivery import result_manifest,export_bundle
from setforge.generators import discover,probe_plan
from setforge.io import read,write,sha
from setforge.project import init_project,check_project
from setforge.stylepack import configure
from setforge.ui import make_server
from setforge.ui_skin import measure_label
from fixture_worker import plan


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'project'
        init_project(self.project)

    def pack(self):
        folder=self.root/'style';folder.mkdir()
        Image.new('RGB',(96,96),'gray').save(folder/'example.png')
        (folder/'style.md').write_text('TEST STYLE PACK — NOT ORIGINAL ART',encoding='utf8')
        write(folder/'style-pack.json',{'version':1,'id':'test-style','references':[{'id':'sample','path':'example.png'}]})
        return folder

    def completed_fixture(self):
        run=self.root/'orders'/'run-fixture';run.mkdir(parents=True)
        value=plan();accepted={};exports=[]
        for variant in value['variants']:
            for index,card in enumerate(variant['cards']):
                key=variant['id']+'/'+card['id'];relative='jobs/'+key.replace('/','-')+'/artifact.png'
                source=run/relative;source.parent.mkdir(parents=True)
                image=Image.new('RGB',(430,480),(index*20,70,100))
                ImageDraw.Draw(image).text((20,20),'TEST FIXTURE - NOT ART',fill='white');image.save(source)
                export='exports/'+key+'.png';target=run/export;target.parent.mkdir(parents=True,exist_ok=True);image.save(target)
                accepted[key]={'path':relative,'sha256':sha(source),'eligible':True,'quality':95,
                               'scores':dict.fromkeys(IDS,95),'stars':card['stars'],'golden':card['golden'],
                               'observed_category':card['background']}
                exports.append({'key':key,'path':export,'sha256':sha(target),'source_sha256':sha(source),'acceptance':'accepted'})
        write(run/'inputs/brief.json',{'theme':'TEST distribution','wishes':'TEST only','variants':3})
        write(run/'inputs/private-reference.json',{'never':'include me'})
        write(run/'exports/manifest.json',exports)
        write(run/'state.json',{'status':'complete','plan':value,'accepted':accepted,'art_acceptance':{'threshold':93.5},
                               'art_stops':dict.fromkeys(accepted,'accepted'),'canon':[],'jobs':[]})
        return run

    def test_empty_project_opens_setup_but_cannot_generate(self):
        server=make_server(self.project,self.root/'orders',0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(method,path,body=None,headers=None):
            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            connection.request(method,path,body,headers or {});response=connection.getresponse()
            result=(response.status,response.read());connection.close();return result
        status,body=request('GET','/');self.assertEqual(status,200);self.assertIn('Свободные пожелания',body.decode())
        status,body=request('GET','/api/context');self.assertFalse(json.loads(body)['ready'])
        status,_=request('POST','/api/runs',json.dumps({'theme':'TEST'}));self.assertEqual(status,400)
        status,_=request('GET','/',headers={'Host':'outside.invalid'});self.assertEqual(status,403)
        configure(self.project,self.pack())
        status,body=request('GET','/api/context');self.assertTrue(json.loads(body)['ready'])
        status,_=request('GET','/runs/%2e%2e/project/project.json');self.assertEqual(status,404)
        run=self.completed_fixture()
        status,body=request('GET','/api/runs');self.assertIn('download',json.loads(body)[0])
        status,body=request('GET','/runs/run-fixture/delivery.zip');self.assertEqual(status,200);self.assertEqual(body[:2],b'PK')
        state=read(run/'state.json');state['accepted']['v1/c1']['quality']=90;write(run/'state.json',state)
        status,_=request('GET','/runs/run-fixture/delivery.zip');self.assertEqual(status,409)

    def test_local_style_import_and_invalid_paths(self):
        folder=self.pack();configure(self.project,folder);config,refs=check_project(self.project)
        self.assertEqual(config['global_reference_ids'],['sample']);self.assertEqual(refs[0]['sha256'],sha(folder/'example.png'))
        spec=read(folder/'style-pack.json');spec['references'][0]['path']='../project/references/sample.png'
        write(folder/'style-pack.json',spec)
        with self.assertRaisesRegex(ValueError,'escapes'):configure(self.project,folder)
        self.assertEqual(check_project(self.project)[1][0]['id'],'sample')

    def test_duplicate_reference_ids_do_not_replace_project(self):
        folder=self.pack();spec=read(folder/'style-pack.json');spec['references']*=2;write(folder/'style-pack.json',spec)
        with self.assertRaisesRegex(ValueError,'distinct'):configure(self.project,folder)
        self.assertEqual(read(self.project/'references.json'),[])

    def test_capability_scan_and_probe_do_not_call_a_model(self):
        configure(self.project,self.pack())
        with patch('urllib.request.urlopen',side_effect=AssertionError('No model/network calls')):
            scan=discover(self.project);self.assertEqual(scan['generators'][0]['state'],'runtime_check_required')
            cap=self.root/'cap.json';write(cap,{'tools':[{'id':'imagegen','generate':True,'edit':True,'reference_images':True}]})
            self.assertEqual(discover(self.project,cap)['generators'][0]['state'],'agent_confirmed')
            result=probe_plan(self.project,self.root/'probe.json',cap)
        self.assertEqual(result['image_generations'],0);self.assertEqual(result['status'],'waiting_for_external_agent')
        self.assertFalse(scan['automatic_switch']);self.assertEqual(len(read(self.root/'probe.json')['tests']),3)

    def test_titles_use_actual_fixed_renderer(self):
        self.assertLess(measure_label('Dice')['width'],160)
        self.assertGreater(measure_label('Strawberry Picnic Basket')['width'],160)
        self.assertEqual(measure_label('Dice')['ink_height'],18)
        self.assertFalse(measure_label('Dice')['fit_to_width'])

    def test_final_zip_is_complete_portable_and_contains_no_history(self):
        run=self.completed_fixture();manifest=result_manifest(run);self.assertTrue(manifest['full_delivery_ready'])
        receipt=export_bundle(run);self.assertEqual(receipt,export_bundle(run))
        with patch('setforge.delivery.presentation_fingerprint',return_value='changed-ui-version'):
            updated=export_bundle(run)
            self.assertNotEqual(receipt['fingerprint'],updated['fingerprint'])
        with zipfile.ZipFile(receipt['path']) as archive:
            names=archive.namelist();self.assertEqual(len([p for p in names if p.startswith('images/')]),30)
            self.assertEqual(len([p for p in names if p.startswith('cards/')]),30)
            self.assertFalse(any(p.startswith(('inputs/','jobs/')) for p in names))
            self.assertIn('report.html',names);self.assertIn('result-manifest.json',names)
            public=json.loads(archive.read('result-manifest.json'))
            self.assertNotIn('_native_source',public['variants'][0]['cards'][0])
            for group in public['variants']:
                for card in group['cards']:
                    self.assertIn(card['path'],names);self.assertIn(card['native_path'],names)
            page=archive.read('report.html').decode()
            self.assertEqual(page.count('data-variant='),3);self.assertNotIn('href="audit.html"',page)
            self.assertNotIn('href="delivery.zip"',page)
            import re
            from urllib.parse import unquote
            for link in re.findall(r'(?:href|src)="([^"]+)"',page):
                if not link.startswith(('#','data:','http:','https:')):self.assertIn(unquote(link),names)

    def test_incomplete_score_category_and_hash_fail_closed(self):
        run=self.completed_fixture();original=read(run/'state.json');key='v1/c1'
        for mutation in ('incomplete','low_score','category','hash','stars','title'):
            state=copy.deepcopy(original)
            if mutation=='incomplete':state['status']='completed_with_rejections';state['accepted'].pop(key)
            if mutation=='low_score':state['accepted'][key]['quality']=93.49
            if mutation=='category':state['accepted'][key]['observed_category']=5
            if mutation=='hash':state['accepted'][key]['sha256']='changed'
            if mutation=='stars':state['accepted'][key]['stars']=5
            if mutation=='title':state['plan']['variants'][0]['cards'][0]['title']='Strawberry Picnic Basket'
            write(run/'state.json',state)
            with self.subTest(mutation=mutation):
                self.assertFalse(result_manifest(run)['full_delivery_ready'])
                with self.assertRaisesRegex(ValueError,'not accepted'):export_bundle(run)
                self.assertFalse((run/'delivery.zip').exists())
        write(run/'state.json',original)
        write(run/'presentation-naming-notes.json',{'producer_flagged_labels':[key]})
        self.assertFalse(result_manifest(run)['full_delivery_ready'])

    def test_optional_api_requires_explicit_access_and_model(self):
        import base64,os,sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        from setforge.image_api import invoke_image_api
        folder=self.root/'api-test';folder.mkdir()
        reference=folder/'reference.png';Image.new('RGB',(32,32),'gray').save(reference)
        request={'prompt':'OFFLINE TEST ONLY','images':[{'path':'reference.png'}]}
        fake=Mock();fake.return_value.images.edit.return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(reference.read_bytes()).decode())])
        with patch.dict(os.environ,{'SETFORGE_TEST_KEY':'TEST_ONLY'}),patch.dict(sys.modules,{'openai':SimpleNamespace(OpenAI=fake)}):
            config={'key_env':'SETFORGE_TEST_KEY'}
            with self.assertRaisesRegex(ValueError,'explicit image API model'):invoke_image_api(config,request,folder,folder,5)
            config['model']='EXPLICIT_TEST_MODEL'
            result=invoke_image_api(config,request,folder,folder,5)
        fake.return_value.images.generate.assert_not_called()
        self.assertEqual(sha(result['image_path']),sha(reference));self.assertEqual(fake.call_args.kwargs['max_retries'],0)

    def test_cli_manifest_exit_and_inbox(self):
        run=self.completed_fixture()
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(['result-manifest','--run',str(run)]),0)
        self.assertTrue(json.loads(stream.getvalue())['full_delivery_ready'])
        state=read(run/'state.json');state['status']='waiting';write(run/'state.json',state)
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(['inbox','--runs',str(run.parent)]),0)
        self.assertEqual(json.loads(stream.getvalue())['orders'][0]['id'],run.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['result-manifest','--run',str(run)]),3)


if __name__=='__main__':unittest.main()
