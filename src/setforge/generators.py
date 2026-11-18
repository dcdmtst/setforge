"""Capability discovery and optional agent-executed canaries; no silent API calls."""
import os
import shutil
from pathlib import Path
from .io import read,write,sha


def discover(project,capabilities=None):
    project=Path(project);config=read(project/'project.json')
    declared=read(capabilities) if capabilities else {}
    tools=declared.get('tools',[])
    builtin=next((x for x in tools if x.get('id')=='imagegen'),{})
    complete=all(builtin.get(k) is True for k in ('generate','edit','reference_images'))
    items=[{'id':'imagegen','default':True,'state':'agent_confirmed' if complete else 'runtime_check_required',
            'generate':builtin.get('generate'),'edit':builtin.get('edit'),
            'reference_images':builtin.get('reference_images'),
            'tested_by_generation':False,'cash_policy':'zero additional cash; agent must verify its own subscription/tool access',
            'reason':'A Codex installation alone is not evidence that this runtime exposes imagegen.'}]
    configured=config.get('generators',[])
    if config['providers']['image']['type']!='files':
        configured=[{'id':'configured-image-provider',**config['providers']['image']},*configured]
    for provider in configured:
        kind=provider['type'];ready=False
        if kind=='command':
            executable=provider.get('argv',[''])[0]
            ready=bool(Path(executable).is_file() or shutil.which(executable) or executable=='{python}')
        elif kind=='image-cli':ready=bool(os.environ.get(provider.get('key_env','OPENAI_API_KEY')))
        items.append({'id':provider['id'],'default':False,'type':kind,
                      'state':'configured_not_tested' if ready else 'configuration_missing',
                      'zero_cost_declared':provider.get('zero_cost',False),
                      'tested_by_generation':False})
    return {'schema_version':1,'default':'imagegen','network_requests':0,'image_generations':0,
            'generators':items,'automatic_switch':False,
            'note':'Capability declarations and credentials are not proof of successful generation. Probe explicitly before selecting another model.'}


def probe_plan(project,out,capabilities=None):
    project=Path(project).resolve();scan=discover(project,capabilities)
    refs=read(project/'references.json')
    if not refs:raise ValueError('A local style pack is required before a fair generator probe.')
    plan={'schema_version':1,'mode':'external_agent_required','max_additional_cash':0,
          'default_unchanged':'imagegen','quality_threshold':93.5,'generators':scan['generators'],
          'reference_pack':[{'id':x['id'],'path':str(project/x['path']),'sha256':sha(project/x['path'])} for x in refs[:3]],
          'tests':[{'id':'materials','prompt':'One small basket of strawberries. Match the supplied style references; dry wicker, soft matte cloth, controlled fruit highlights, complete silhouette. No game UI.'},
                   {'id':'scene','prompt':'Two clearly distinct family characters sharing one tabletop game move. Match supplied style, coherent hands, attachments and contacts; complete main group, no game UI.'},
                   {'id':'edit','prompt':'Edit the materials candidate: simplify pervasive fine texture and reduce broad lacquer-like reflections while preserving the object, palette and background.'}],
          'result_contract':{'per_generator':['exact prompts and input hashes','PNG paths/SHA256','actual call start/end','additional cash','all nine scores and hard-gate findings from an independent visual reviewer','number of edits','accepted or rejected'],
                             'selection':'Compare accepted outcome time including corrections, quality and cash. Do not automatically replace imagegen.'}}
    write(out,plan)
    return {'probe':str(Path(out).resolve()),'status':'waiting_for_external_agent','image_generations':0,
            'message':'The agent executes this optional zero-cash probe using its available tools. This command itself does not contact a model.'}
