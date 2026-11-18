"""CLI/worker boundary checks using labelled OFFLINE fixtures, never real art."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from setforge.cli import main
from setforge.io import read, write
from setforge.workqueue import WorkQueue
from setforge.workers import work_once
from fixture_worker import respond
import test_pipeline as legacy
import test_parallel as parallel_tests


class QueueCLITests(unittest.TestCase):
    setUp = parallel_tests.ParallelTests.setUp
    make = parallel_tests.ParallelTests.make
    advance = parallel_tests.ParallelTests.advance
    request = parallel_tests.ParallelTests.request
    answer = parallel_tests.ParallelTests.answer
    next_claim = parallel_tests.ParallelTests.next_claim
    to_art = parallel_tests.ParallelTests.to_art

    def test_cli_scaling_files_worker_and_no_submit_lease_bypass(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code=main(['run','--project',str(self.project),'--theme','TEST theme',
                       '--artists','8','--run',str(self.run)])
        self.assertEqual(code,3)
        self.assertEqual(read(self.run/'state.json')['orchestration']['artists'],8)
        before=WorkQueue(self.run).jobs()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code=main(['worker','--run',str(self.run),'--worker','external-files','--role','reviewer','--once'])
        self.assertEqual(code,2)
        self.assertEqual(WorkQueue(self.run).jobs(),before)
        job=before[0]
        response=self.root/'answer.json';write(response,respond(read(self.run/'jobs'/job['id']/'request.json'),self.run))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code=main(['submit','--run',str(self.run),'--job',job['id'],'--executor','bypass','--response',str(response)])
        self.assertEqual(code,2)
        self.assertEqual(WorkQueue(self.run).jobs()[0]['status'],'ready')

    def worker_config(self,path):
        value=read(path)
        if Path(path)==self.run/'inputs/project.json':
            value['providers']['image']={'type':'command','estimated_cost':0,'zero_cost':True,'argv':['TEST-NOT-EXECUTED']}
        return value

    def test_unknown_image_outcome_holds_capacity_without_mutating_state(self):
        self.to_art()
        before=(self.run/'state.json').read_bytes()
        with patch('setforge.workers.read',side_effect=self.worker_config), patch('setforge.workers.invoke',side_effect=TimeoutError('TEST uncertain timeout')) as invoke:
            result=work_once(self.run,'other-artist','image')
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(invoke.call_count,1)
        self.assertIsNone(self.queue.claim('third-artist','image'))
        self.assertEqual((self.run/'state.json').read_bytes(),before)

    def test_worker_ignores_transient_local_attention_during_coordinator_apply(self):
        first=self.to_art()
        state=read(self.run/'state.json')
        state.update(status='needs_attention',reason='TEST transient local card stop',
                     _queue_applying={'id':first['id'],'card':first['card'],'kind':'card_review'})
        write(self.run/'state.json',state)
        before=(self.run/'state.json').read_bytes()
        with patch('setforge.workers.read',side_effect=self.worker_config), patch('setforge.workers.invoke',side_effect=lambda cfg,req,run,job,timeout:respond(req,run)) as invoke:
            result=work_once(self.run,'independent-artist','image')
        self.assertEqual(result['status'],'delivered')
        self.assertEqual(invoke.call_count,1)
        self.assertEqual((self.run/'state.json').read_bytes(),before)

    def test_provider_worker_renews_lease_during_slow_invocation(self):
        self.to_art()
        observed=[]
        def slow_provider(config,request,run,job,timeout):
            time.sleep(.65)
            lease=next(j for j in self.queue.jobs() if j['worker']=='slow-artist')
            observed.append(lease['status'])
            return respond(request,run)
        with patch('setforge.workers.read',side_effect=self.worker_config), patch('setforge.workers.invoke',side_effect=slow_provider):
            result=work_once(self.run,'slow-artist','image',lease_seconds=.3)
        self.assertEqual(observed,['leased'])
        self.assertEqual(result['status'],'delivered')

    def test_durable_result_recovers_with_original_lease_without_second_call(self):
        self.to_art()
        with patch('setforge.workers.read',side_effect=self.worker_config), patch('setforge.workers.invoke',side_effect=lambda cfg,req,run,job,timeout:respond(req,run)) as invoke, patch.object(WorkQueue,'deliver',side_effect=OSError('TEST inbox unavailable')):
            result=work_once(self.run,'other-artist','image')
        self.assertEqual(result['status'],'uncertain')
        saved=Path(result['execution'])/'worker-response.json'
        self.assertTrue(saved.is_file())
        lease=next(j for j in self.queue.jobs() if j['id']==result['job'])
        recovered=self.queue.deliver(lease['id'],'other-artist',lease['token'],read(saved))
        self.assertEqual(recovered['status'],'submitted')
        self.assertEqual(invoke.call_count,1)

    def test_subprocess_coordinator_and_workers_overlap_and_finish_thirty(self):
        # This tiny wrapper only produces the labelled fixture patterns and QA
        # from fixture_worker.py. It logs real provider-process intervals.
        worker=self.root/'slow-fixture.py'
        worker.write_text('''import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,'''+repr(str(Path(__file__).parent))+''')
from fixture_worker import respond
p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--response');a=p.parse_args()
request=json.loads(Path(a.request).read_text(encoding='utf8'));run=Path.cwd()
started=time.monotonic();owner=None
try:owner=int((run/'.lock').read_text())
except (OSError,ValueError):pass
if request['kind']=='image':time.sleep(.2)
answer=respond(request,run)
Path(a.response).write_text(json.dumps(answer),encoding='utf8')
trace=run/'fixture-invocations';trace.mkdir(exist_ok=True)
(trace/(request['job_id']+'.json')).write_text(json.dumps({'job':request['job_id'],'kind':request['kind'],'started':started,'ended':time.monotonic(),'lock_owner':owner,'worker_pid':os.getppid()}),encoding='utf8')
''',encoding='utf8')
        for role in ('image','reviewer','planner'):
            self.config['providers'][role]={'type':'command','argv':['{python}',str(worker),'--request','{request}','--response','{response}'],
                                            'estimated_cost':0,'zero_cost':True,'supports_images':True}
        write(self.project/'project.json',self.config)
        create=subprocess.run([sys.executable,'-m','setforge','run','--project',str(self.project),'--theme','TEST concurrent fixture',
                               '--artists','2','--run',str(self.run)],capture_output=True,text=True,encoding='utf8',timeout=20)
        self.assertEqual(create.returncode,3,create.stderr)
        processes=[];logs=[]
        def start(args,name):
            log=(self.root/(name+'.log')).open('w',encoding='utf8');logs.append(log)
            process=subprocess.Popen([sys.executable,'-m','setforge',*args],stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0),
                                     start_new_session=os.name!='nt')
            processes.append(process);return process
        try:
            coordinator=start(['coordinate','--run',str(self.run),'--watch','--poll-seconds','.02'],'coordinator')
            for name,role in [('artist-1','image'),('artist-2','image'),('art-director','reviewer'),('planner','planner')]:
                start(['worker','--run',str(self.run),'--worker',name,'--role',role,'--poll-seconds','.02'],name)
            coordinator.wait(timeout=150)
            self.assertEqual(coordinator.returncode,0,(self.root/'coordinator.log').read_text(encoding='utf8')[-4000:])
            state=read(self.run/'state.json')
            self.assertEqual(state['status'],'complete')
            self.assertEqual(len(state['accepted']),30)
            records=[read(p) for p in (self.run/'fixture-invocations').glob('*.json')]
            images=[r for r in records if r['kind']=='image']
            self.assertEqual(len(images),30)
            self.assertTrue(all(r['lock_owner']!=r['worker_pid'] for r in records),'Provider was invoked by a worker holding the state lock')
            self.assertTrue(any(a['started']<b['started']<a['ended'] for a in images for b in images if a['job']!=b['job']),
                            'Two image providers never overlapped')
            for process in processes[1:]:
                process.wait(timeout=15)
                self.assertEqual(process.returncode,0)
            write(legacy.ROOT/'work/queue-cli-e2e.json',{
                'verdict':'pass','evidence_type':'labelled offline infrastructure fixtures; not real art QA',
                'accepted_cards':len(state['accepted']),'actual_image_provider_processes':len(images),
                'provider_processes_outside_worker_run_lock':all(r['lock_owner']!=r['worker_pid'] for r in records),
                'image_provider_intervals_overlap':True,'image_intervals':images,
                'worker_processes':4,'coordinator_processes':1,
                'worker_exit_codes':[process.returncode for process in processes[1:]],
                'coordinator_exit_code':coordinator.returncode})
        finally:
            # Preserve diagnosis even when temporary fixture directories are
            # cleaned up; these files contain only explicit offline test data.
            for log in logs:log.flush()
            debug={'process_exit_codes':[p.poll() for p in processes]}
            try:debug['state']=read(self.run/'state.json')
            except (OSError,ValueError):pass
            try:debug['queue']=WorkQueue(self.run).jobs()
            except (OSError,ValueError):pass
            debug['logs']={p.name:p.read_text(encoding='utf8')[-8000:] for p in self.root.glob('*.log')}
            write(legacy.ROOT/'work/queue-cli-e2e-debug.json',debug)
            for process in processes:
                if process.poll() is None:
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,timeout=10)
                    else:
                        import signal
                        os.killpg(process.pid,signal.SIGTERM)
            for process in processes:
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            for log in logs:log.close()


if __name__=='__main__':unittest.main()
