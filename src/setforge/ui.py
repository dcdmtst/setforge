"""Local producer form. Uses the same persisted pipeline, not a second execution path."""
import json
import mimetypes
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .engine import Pipeline, NeedsInput
from .io import read, write, locked, sha
from .project import check_project
from .report import report
from .schemas import BRIEF, validate


def project_context(project):
    config=read(Path(project)/'project.json')
    try:
        check_project(project);ready=True;issues=[]
    except (ValueError,OSError,KeyError) as error:
        ready=False;issues=[str(error)]
    return {'project':config.get('collection_id','collection'),'ready':ready,'issues':issues,
            'external_agent_required':any(p.get('type')=='files' for p in config.get('providers',{}).values()),
            'default_variants':3,'cards_per_set':10}


def orders(runs):
    result=[];runs=Path(runs)
    if not runs.exists():return result
    for folder in sorted(runs.iterdir(),reverse=True):
        if not (folder/'state.json').is_file():continue
        try:
            state=read(folder/'state.json');brief=read(folder/'inputs/brief.json')
            item={'id':folder.name,'theme':brief['theme'],'variants':brief.get('variants',3),
                  'ready_cards':len(state['accepted']),'status':state['status'],
                  'report':f'/runs/{folder.name}/report.html','run':str(folder.resolve())}
            if state['status']=='complete':item['download']=f'/runs/{folder.name}/delivery.zip'
            result.append(item)
        except (OSError,ValueError,KeyError):continue
    return result


def create_order(project, runs, value, *, artists=2,reviewers=2,workers=None):
    if not isinstance(value, dict) or set(value)-{'theme','wishes','variants'}:
        raise ValueError('Expected theme, free-form wishes and variant count.')
    brief={'theme':value.get('theme',''),'wishes':value.get('wishes',''),
           'variants':value.get('variants',3),'production_mode':'full'}
    validate(brief,BRIEF)
    if not brief['theme'].strip():raise ValueError('Укажите тему набора.')
    runs=Path(runs).resolve();runs.mkdir(parents=True,exist_ok=True)
    name='run-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    pipeline=Pipeline.create(project,brief,runs/name,artists=artists,reviewers=reviewers,workers=workers)
    write(pipeline.run/'producer-brief.json',brief)
    report(pipeline.run,pipeline.state)
    return pipeline


def advance_order(run):
    from .parallel import load
    from .workers import work
    pipeline=load(run)
    if 'orchestration' not in pipeline.state:
        with locked(run):
            pipeline=load(run)
            try:pipeline.advance()
            except NeedsInput:pass
            finally:report(pipeline.run,pipeline.state)
        return
    stop=threading.Event()
    threads=[]
    def worker_loop(name,role):
        try:work(run,name,role,stop_event=stop)
        except (ValueError,OSError,KeyError,sqlite3.Error) as error:
            write(Path(run)/'worker-logs'/f'{name}.json',{'error':str(error),'role':role})
    # Form-driven automatic adapters use the same worker/lease interface as CLI.
    # File adapters are left for external agents; the form does not invoke chat tools.
    for role,provider in pipeline.config['providers'].items():
        if provider['type']=='files':continue
        count=pipeline.state['orchestration']['artists'] if role=='image' else pipeline.state['orchestration'].get('reviewers',1) if role=='reviewer' else 1
        for i in range(count):
            name=f'ui-{role}-{i+1}'
            thread=threading.Thread(target=worker_loop,args=(name,role),daemon=True)
            thread.start();threads.append(thread)
    try:
        while True:
            with locked(run):
                pipeline=load(run)
                before=sha(Path(run)/'state.json')
                try:pipeline.advance()
                except NeedsInput:pass
                except (ValueError,OSError,KeyError,sqlite3.Error) as error:
                    pipeline.state.update(status='needs_attention',reason=str(error))
                    pipeline.save()
                if sha(Path(run)/'state.json')!=before:
                    report(pipeline.run,pipeline.state)
                terminal=pipeline.state['status'] in ('complete','completed_with_rejections','needs_attention','waiting_for_selection')
            if terminal or not threads:
                if pipeline.state['status']=='complete':
                    from .delivery import export_bundle
                    export_bundle(run)
                    report(pipeline.run,pipeline.state)
                return
            stop.wait(.25)
    finally:
        stop.set()


def make_server(project,runs,port=8840,*,artists=2,reviewers=2,workers=None):
    project=Path(project).resolve();runs=Path(runs).resolve()
    read(project/'project.json')
    runs.mkdir(parents=True,exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_data(self,value,status=200,content_type='application/json; charset=utf-8'):
            payload=json.dumps(value,ensure_ascii=False).encode() if isinstance(value,(dict,list)) else value
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(payload)
        def valid_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        def do_GET(self):
            if not self.valid_host():return self.send_data({'error':'Unexpected host'},403)
            path=urlsplit(self.path).path
            if path=='/':
                return self.send_data((files('setforge')/'defaults/producer-form.html').read_bytes(),content_type='text/html; charset=utf-8')
            if path=='/api/context':
                return self.send_data({**project_context(project),'artists':artists,'reviewers':reviewers,'workers':workers or artists+1})
            if path=='/api/runs':
                return self.send_data(orders(runs))
            if path.startswith('/runs/'):
                target=(runs/unquote(path[len('/runs/'):])).resolve()
                if target.is_relative_to(runs) and target.name=='result-manifest.json' and target.parent.parent==runs:
                    try:
                        from .delivery import save_manifest
                        save_manifest(target.parent)
                    except (ValueError,OSError,KeyError) as error:return self.send_data({'error':str(error)},409)
                if target.is_relative_to(runs) and target.name=='delivery.zip' and target.parent.parent==runs:
                    try:
                        from .delivery import export_bundle
                        export_bundle(target.parent)
                    except (ValueError,OSError,KeyError) as error:return self.send_data({'error':str(error)},409)
                if target.is_relative_to(runs) and target.is_file():
                    mime=mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
                    return self.send_data(target.read_bytes(),content_type=mime)
            self.send_data({'error':'Not found'},404)
        def do_POST(self):
            host=self.headers.get('Host','')
            origin=self.headers.get('Origin')
            if not self.valid_host() or (origin and urlsplit(origin).netloc!=host):
                # Drain a small, already submitted body before closing HTTP/1.0.
                # Otherwise Windows can reset the connection before the client
                # receives the 403 response because unread bytes remain.
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:size=0
                if 0<size<=262144:
                    previous_timeout=self.connection.gettimeout()
                    self.connection.settimeout(1)
                    try:self.rfile.read(size)
                    except OSError:pass
                    finally:self.connection.settimeout(previous_timeout)
                return self.send_data({'error':'Only this local panel may submit an order.'},403)
            if urlsplit(self.path).path!='/api/runs':return self.send_data({'error':'Not found'},404)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=262144:raise ValueError('Размер брифа превышает допустимый предел.')
                value=json.loads(self.rfile.read(size))
                context=project_context(project)
                if not context['ready']:raise ValueError('Setup incomplete: '+'; '.join(context['issues']))
                pipeline=create_order(project,runs,value,artists=artists,reviewers=reviewers,workers=workers)
                threading.Thread(target=advance_order,args=(pipeline.run,),daemon=True).start()
                self.send_data({'id':pipeline.run.name,'report':f'/runs/{pipeline.run.name}/report.html'},201)
            except (ValueError,OSError,KeyError) as error:
                self.send_data({'error':str(error)},400)
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(project,runs,port=8840,*,artists=2,reviewers=2,workers=None):
    server=make_server(project,runs,port,artists=artists,reviewers=reviewers,workers=workers)
    print(f'Setforge: http://127.0.0.1:{server.server_port}/',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
