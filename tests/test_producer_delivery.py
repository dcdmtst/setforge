"""Producer inputs and full-delivery contract; fixtures do not validate actual art."""
import contextlib
import io
import json
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.client import HTTPConnection
from setforge.cli import main
from setforge.engine import NeedsInput
from setforge.io import read,write
from setforge.report import export
from setforge.showcase import render
from setforge.ui import create_order,make_server
from fixture_worker import plan,review
import test_pipeline as legacy


class ProducerDeliveryTests(unittest.TestCase):
    setUp=legacy.PipelineTests.setUp
    make=legacy.PipelineTests.make
    wait=legacy.PipelineTests.wait
    deliver=legacy.PipelineTests.deliver
    to_images=legacy.PipelineTests.to_images
    finish=legacy.PipelineTests.finish

    def test_default_three_full_variants_ignore_old_selection_switch(self):
        self.config['rules']['select_before_art']=True
        write(self.project/'project.json',self.config)
        pipe=self.make()
        request=self.to_images(pipe)
        self.assertEqual(request['kind'],'image')
        self.assertEqual(pipe.state['production_contract']['requested_variants'],3)
        self.assertEqual(len(pipe.cards()),30)
        with self.assertRaisesRegex(ValueError,'All requested variants'):
            pipe.choose_plan('v1','Silently reduce output','test')

    def test_requested_five_sets_complete_as_fifty_pngs(self):
        self.brief['variants']=5
        pipe=self.make()
        self.finish(pipe)
        self.assertEqual(len(pipe.state['accepted']),50)
        self.assertEqual(len(read(self.run/'exports/manifest.json')),50)
        page=(self.run/'report.html').read_text(encoding='utf-8')
        self.assertEqual(page.count('data-variant='),5)
        self.assertIn('50 / 50',page)

    def test_wishes_are_verbatim_and_have_review_gates(self):
        wish='Уютный праздник.\nДве сценки с персонажами, без золотых карт. "Не терять это".'
        self.brief['wishes']=wish
        self.config['rules'].update(exclude_main_cast_scenes=False,exclude_golden_cards=True)
        write(self.project/'project.json',self.config)
        pipe=self.make()
        for _ in range(2):
            request=self.wait(pipe)
            self.assertEqual(request['context']['brief']['wishes'],wish)
            self.deliver(pipe)
        request=self.wait(pipe)
        self.assertIn('producer_wishes',request['required_criteria'])
        self.assertEqual(read(self.run/'inputs/brief.json')['wishes'],wish)
        answer=review(request)
        answer['criteria']=[c for c in answer['criteria'] if c['name']!='producer_wishes']
        with self.assertRaisesRegex(ValueError,'required criteria'):
            pipe.accept(answer)

    def test_wish_failure_cannot_reach_art(self):
        self.brief['wishes']='TEST a concrete wished-for feature'
        pipe=self.make()
        for _ in range(2):self.wait(pipe);self.deliver(pipe)
        request=self.wait(pipe)
        answer=review(request)
        for criterion in answer['criteria']:
            if criterion['name']=='producer_wishes':criterion['verdict']='fail'
        answer.update(verdict='fail',action='revise')
        pipe.accept(answer)
        self.assertEqual(pipe.state['phase'],'plan')
        self.assertFalse(any(j['kind']=='image' for j in pipe.state['jobs']))

    def test_no_gold_does_not_prohibit_required_characters(self):
        self.config['rules'].update(exclude_main_cast_scenes=False,exclude_golden_cards=True)
        write(self.project/'project.json',self.config)
        self.brief['characters']='required'
        pipe=self.make()
        data=plan()
        for v in data['variants']:
            v['cards'][-1].update(background=4,golden=False,main_cast_scene=True,key_character_count=2)
        pipe.validate_plan(data)
        for v in data['variants']:v['cards'][-1].update(background=5,golden=True)
        with self.assertRaisesRegex(ValueError,'Golden cards are excluded'):
            pipe.validate_plan(data)

    def test_all_variants_remain_visible_when_only_one_has_art(self):
        from PIL import Image
        image=self.root/'native.png';Image.new('RGB',(430,480),'green').save(image)
        state={'plan':plan(),'accepted':{f'v1/c{i}':{'path':'native.png'} for i in range(1,11)}}
        result=render(self.root,state,{'theme':'TEST partial','wishes':'TEST wishes','variants':3},audit_path=None)
        page=(self.root/'report.html').read_text(encoding='utf-8')
        self.assertEqual(result['ready_sets'],1)
        self.assertEqual(result['expected_cards'],30)
        self.assertFalse(result['full_delivery_ready'])
        self.assertEqual(page.count('data-variant='),3)
        self.assertEqual(page.count('Арт ещё не создан'),20)

    def test_missing_requested_sets_cannot_export_as_complete(self):
        pipe=self.make()
        pipe.state['plan']=plan()
        pipe.state['production_variant_ids']=['v1']
        pipe.state['accepted']={f'v1/c{i}':{} for i in range(1,11)}
        self.assertEqual(len(pipe.cards()),30)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            export(self.run,pipe.state,pipe.config)

    def test_cli_wishes_and_variant_inputs(self):
        target=self.root/'brief.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['brief','--theme','TEST','--wishes','Свободно\nДословно','--out',str(target)]),0)
        self.assertEqual(read(target),{'theme':'TEST','wishes':'Свободно\nДословно','variants':3})
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['run','--project',str(self.project),'--theme','TEST','--wishes','Точный текст',
                                  '--variants','4','--run',str(self.run)]),3)
        self.assertEqual(read(self.run/'inputs/brief.json')['variants'],4)
        self.assertEqual(read(self.run/'inputs/brief.json')['wishes'],'Точный текст')

    def test_local_form_creates_real_order_without_model_calls(self):
        runs=self.root/'orders'
        server=make_server(self.project,runs,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}'
        def request(method,path,body=None,origin=None):
            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            connection.request(method,path,body,headers)
            response=connection.getresponse();data=response.read();status=response.status
            connection.close()
            return status,data
        status,data=request('GET','/')
        self.assertEqual(status,200)
        self.assertIn('Свободные пожелания',data.decode())
        value={'theme':'TEST form','wishes':'  Дословно\nС персонажами  ','variants':4}
        status,data=request('POST','/api/runs',json.dumps(value).encode(),url)
        self.assertEqual(status,201)
        data=json.loads(data)
        brief=read(runs/data['id']/'inputs/brief.json')
        self.assertEqual(brief['wishes'],value['wishes'])
        self.assertEqual(brief['variants'],4)
        self.assertEqual(brief['production_mode'],'full')
        status,_=request('POST','/api/runs',json.dumps(value).encode(),'https://outside.invalid')
        self.assertEqual(status,403)


if __name__=='__main__':unittest.main()
