"""Install an isolated local app. Does not configure a model or call one."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import venv
import webbrowser


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--style-pack',type=Path)
    parser.add_argument('--port',type=int,default=8840)
    parser.add_argument('--start',action='store_true',help='Start the local page in the background')
    parser.add_argument('--open',action='store_true',help='Open the page after a successful start')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];data=root/'.setforge';data.mkdir(exist_ok=True)
    env=root/'.venv';python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.exists():venv.EnvBuilder(with_pip=True).create(env)
    subprocess.run([str(python),'-m','pip','install','-e',str(root)],check=True)
    base=[str(python),'-m','setforge'];project=data/'project';runs=data/'runs'
    if not (project/'project.json').exists():
        subprocess.run([*base,'init','--project',str(project)],check=True)
    if args.style_pack:
        subprocess.run([*base,'import-style','--project',str(project),'--style-pack',str(args.style_pack.resolve())],check=True)
    url=f'http://127.0.0.1:{args.port}/';command=[*base,'ui','--project',str(project),'--runs',str(runs),'--port',str(args.port)]
    if args.start:
        context=None
        try:
            with urlopen(url+'api/context',timeout=1) as response:context=json.load(response)
        except OSError:pass
        if context is not None:
            raise SystemExit('A server already uses this port. Reuse it, or choose another --port; no process was stopped.')
        with (data/'server.log').open('ab') as log:
            options={'cwd':str(root),'stdout':log,'stderr':subprocess.STDOUT,'stdin':subprocess.DEVNULL}
            if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
            else:options['start_new_session']=True
            process=subprocess.Popen(command,**options)
        (data/'server.json').write_text(json.dumps({'pid':process.pid,'url':url,'project':str(project),'command':command},indent=2),encoding='utf8')
        for _ in range(40):
            if process.poll() is not None:raise SystemExit('Server exited. Read .setforge/server.log.')
            try:
                with urlopen(url+'api/context',timeout=1) as response:context=json.load(response)
                break
            except OSError:time.sleep(.25)
        else:raise SystemExit('Server readiness check timed out. Read .setforge/server.log.')
        print(json.dumps({'url':url,'project_ready':context['ready'],'issues':context['issues'],
                          'external_agent_required':context['external_agent_required']},ensure_ascii=False,indent=2))
        if args.open:webbrowser.open(url)
    else:
        print('Installed. To keep the page running, use: '+' '.join(command),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
