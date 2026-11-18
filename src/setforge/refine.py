"""Scored, resumable revision loops. Historical production runs remain immutable."""
from pathlib import Path
from datetime import datetime,timezone
import json,hashlib,shutil,math
from PIL import Image,ImageOps

from .acceptance import IDS, WEIGHTS, score, decide, THRESHOLD, policy, plateau_signal
from .allocation import check as check_allocation, require_gate
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def write(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def pixels(p):
 with Image.open(p) as im:
  rgba=im.convert('RGBA');return hashlib.sha256(str(rgba.size).encode()+rgba.tobytes()).hexdigest()

def saved_policy(state):
 """Legacy refinement state has scalar v1 settings, never implicit v2 defaults."""
 if 'art_acceptance' in state:return state['art_acceptance']
 snapshot=policy(1)
 for key in ['threshold','max_edits','plateau_delta','plateau_nonimproving_edits']:
  if key in state:snapshot[key]=state[key]
 return snapshot

def preview(src,folder):
 with Image.open(src) as im:
  im.load()
  if im.format!='PNG' or im.width<430 or im.height<480:raise ValueError('Native PNG >=430x480 required')
  if abs((im.width/im.height)/(43/48)-1)>.03:raise ValueError('Incorrect aspect; do not crop subject')
  for size,name in [((430,480),'card.png'),((108,120),'thumb.png')]:
   ImageOps.pad(im.convert('RGB'),size,method=Image.Resampling.LANCZOS,color=(238,242,245)).save(folder/name)

def start(run,source,audit,allocation=None):
 run=Path(run).resolve();source=Path(source).resolve();audit=Path(audit).resolve()
 allocation_file=Path(allocation).resolve() if allocation else source/'allocation.json'
 if not allocation_file.is_file():raise ValueError('Assign and review stars/categories before refinement; supply --allocation PATH_TO_ALLOCATION_JSON.')
 matrix=read(allocation_file)
 if run.exists():raise ValueError('Refinement folder already exists; resume instead')
 run.mkdir(parents=True);write(run/'inputs/rubric.json',read(audit/'protocol.json'))
 reviews=read(audit/'lunar-new-year-scores.json');notes=read(audit/'lny-observations.json');plan=read(source/'run/plans.json')
 manifest=read(source/'final/manifest.json')
 if len(manifest)!=10 or len(reviews)!=10 or len(notes)!=10:raise ValueError('The pilot import requires exactly ten images, scores and feedback records.')
 rows={c['id']:c for c in matrix['variants'][0]['cards']}
 if {c['id'] for c in plan['variants'][0]['cards']}!=set(rows):raise ValueError('Allocation card IDs do not match the source.')
 plan={'variants':[plan['variants'][0]]}
 plan['variants'][0]['cast_reason']=matrix['variants'][0]['cast_reason']
 for c in plan['variants'][0]['cards']:
  c.update({k:rows[c['id']][k] for k in ['stars','golden','background','key_character_count','category_reason','rarity_reason']})
 checked=check_allocation(plan,matrix['policy'])
 if matrix.get('verdict')!='pass':raise ValueError('Allocation must be reviewed before refinement.')
 write(run/'inputs/plans.json',plan)
 snapshot=policy()
 state={'version':2,'art_acceptance':snapshot,'threshold':snapshot['threshold'],'max_edits':snapshot['max_edits'],'plateau_delta':snapshot['plateau_delta'],'plateau_nonimproving_edits':snapshot['plateau_nonimproving_edits'],'cash_limit':0,'model':'imagegen','source':str(source),'cards':{},'pending':None,'status':'running','created_utc':datetime.now(timezone.utc).isoformat()}
 state.update(plan=plan,allocation_policy=matrix['policy'],allocation_gate={'verdict':'pass','plan_sha256':checked['plan_sha256']})
 write(run/'allocation.json',checked)
 refs=run/'inputs/references';refs.mkdir(parents=True)
 for n in [14,43,69,53,56,97,137]:
  f=source.parent/'collection-project/references'/f'CardC6_card_{n:02d}.png';shutil.copy2(f,refs/f.name)
 for m,r,n in zip(manifest,reviews,notes):
  cid=m['key'].split('/')[-1];folder=run/'cards'/cid/'iteration-00';folder.mkdir(parents=True)
  src=source/m['native'];assert digest(src)==m['native_sha256'];shutil.copy2(src,folder/'native.png');preview(folder/'native.png',folder)
  scores={k:r['scores'][k] for k in IDS};entry={'iteration':0,'image':str((folder/'native.png').relative_to(run)).replace('\\','/'),'sha256':digest(folder/'native.png'),'scores':scores,'quality':score(scores),'eligible':not r['blockers'] and r['class_gate'].startswith('pass'),'blockers':r['blockers'],'summary':r['evidence'],'feedback':n['fix'],'strategy':'baseline audit','generation_seconds':0,'source_audit':str(audit/'lunar-new-year-scores.json')}
  entry['eligible']=entry['eligible'] and m.get('observed_background')==rows[cid]['background']
  if m.get('observed_background')!=rows[cid]['background']:entry['feedback']+=' Restore approved illustration category '+str(rows[cid]['background'])+'.'
  entry['plateau_signal']=False
  write(folder/'review.json',entry);state['cards'][cid]={'title':r['title'],'history':[entry],'best':0,'status':decide([entry],acceptance_policy=snapshot),'plateau_signal':False,'next_feedback':entry['feedback'],'next_strategy':'targeted simplification of materials and game volumes'}
 write(run/'policy.json',{**snapshot,'comparison':'unrounded Q95 >= 93.5','max_edits_per_card':snapshot['max_edits'],'plateau':'Two consecutive edits with <0.5 improvement in best eligible score signal a required strategy change. Continue remaining edits; plateau never accepts or stops a v2 card.','full_and_thumbnail_review_required':True,'canonical_dependency':'Revise lantern first; preserve all identity invariants. Recheck existing doorway against the new selected lantern before final delivery.','rubric_weights':dict(zip(IDS,WEIGHTS)),'zero_cash':True})
 write(run/'state.json',state);return status(run)

def status(run):
 s=read(Path(run)/'state.json');snapshot=saved_policy(s);accepted=bool(s['cards']) and all(v['status']=='accepted' and v['history'][v['best']]['eligible'] and v['history'][v['best']]['quality']>=snapshot['threshold'] for v in s['cards'].values())
 terminal=s['status'] in ('finished','stopped_unaccepted')
 return {'status':'stopped_unaccepted' if terminal and not accepted else s['status'],'all_accepted':accepted,'pending':s['pending'],'art_acceptance':snapshot,'cards':{k:{'status':v['status'],'best_quality':v['history'][v['best']]['quality'],'edits':len(v['history'])-1,'plateau_signal':v.get('plateau_signal',False)} for k,v in s['cards'].items()}}

def next_job(run):
 run=Path(run).resolve();s=read(run/'state.json')
 if s['pending']:
  if s['pending']['kind']=='review':return review_request((run/s['pending']['request']).parent,s['pending']['job_id'],'allocation_policy' in s)
  if 'allocation_policy' not in s:raise ValueError('Assign category/star allocation before any further image generation.')
  require_gate(s)
  return read(run/s['pending']['request'])
 order=['lantern']+[k for k in s['cards'] if k!='lantern']
 cid=next((k for k in order if s['cards'][k]['status']=='revise'),None)
 if cid is None:
  s['status']='finished' if all(c['status']=='accepted' for c in s['cards'].values()) else 'stopped_unaccepted'
  write(run/'state.json',s);return status(run)
 if 'allocation_policy' not in s:raise ValueError('Legacy refinement has no category/star allocation; amend metadata before further image edits.')
 require_gate(s)
 c=s['cards'][cid];it=len(c['history']);best=c['history'][c['best']];folder=run/'cards'/cid/f'iteration-{it:02d}';folder.mkdir(parents=True,exist_ok=True)
 card=next(x for x in s['plan']['variants'][0]['cards'] if x['id']==cid)
 snapshot=saved_policy(s)
 job={'job_id':cid+f'-edit-{it:02d}','kind':'image','card':cid,'iteration':it,'edit_target':str(run/best['image']),'edit_target_sha256':best['sha256'],'brief':card,'feedback':c['next_feedback'],'strategy':c['next_strategy'],'previous_scores':best['scores'],'previous_quality':best['quality'],'original_references':[str(x) for x in (run/'inputs/references').glob('*.png')],'cash_limit':0,'threshold':snapshot['threshold'],'art_acceptance':snapshot,'plateau_signal':c.get('plateau_signal',False),'strategy_change_required':snapshot['version']>=2 and c.get('plateau_signal',False),'output':str(folder/'native.png')}
 job['allocation_policy']=s['allocation_policy']
 request=folder/'request.json';write(request,job);s['pending']={'job_id':job['job_id'],'kind':'image','request':request.relative_to(run).as_posix(),'card':cid,'iteration':it};write(run/'state.json',s);return job

def review_request(folder,job_id,allocated=False):
 fields=['scores','evidence','blockers','class_pass','canon_pass','full_inspected','thumbnail_inspected','summary','feedback','next_strategy']
 if allocated:fields+=['observed_category','category_evidence']
 return {'job_id':job_id,'kind':'review','native':str(folder/'native.png'),'delivery':str(folder/'card.png'),'thumbnail':str(folder/'thumb.png'),'required_dimensions':IDS,'required_review_fields':fields}

def attach(run,job_id,source,seconds=None):
 run=Path(run).resolve();s=read(run/'state.json');p=s['pending']
 if not p or p['job_id']!=job_id or p['kind']!='image':raise ValueError('Not the pending image job')
 folder=(run/p['request']).parent;src=Path(source).resolve();target=folder/'native.png'
 if target.exists():raise ValueError('Output exists; no overwrite')
 if digest(src) in {r['sha256'] for c in s['cards'].values() for r in c['history']}:raise ValueError('Unchanged/duplicate pixels file')
 if any(pixels(src)==pixels(run/r['image']) for c in s['cards'].values() for r in c['history']):raise ValueError('Unchanged pixels with different encoding')
 shutil.copy2(src,target);preview(target,folder);write(folder/'generation.json',{'tool':'imagegen','source':str(src),'seconds':seconds,'cash_spend':0,'sha256':digest(target)})
 p['kind']='review';write(run/'state.json',s);return review_request(folder,job_id,'allocation_policy' in s)

def review(run,job_id,response):
 run=Path(run).resolve();s=read(run/'state.json');p=s['pending'];r=read(response)
 if not p or p['job_id']!=job_id or p['kind']!='review':raise ValueError('Not the pending review')
 allowed={'scores','evidence','blockers','class_pass','canon_pass','full_inspected','thumbnail_inspected','summary','feedback','next_strategy'}
 if 'allocation_policy' in s:allowed|={'observed_category','category_evidence'}
 if set(r)!=allowed:raise ValueError('Invalid review fields; derived scores and image paths cannot be supplied by the reviewer')
 if any(type(r[k]) is not bool for k in ['class_pass','canon_pass','full_inspected','thumbnail_inspected']):raise ValueError('Gate fields must be booleans')
 if not isinstance(r['blockers'],list):raise ValueError('Blockers must be a list')
 if not r['full_inspected'] or not r['thumbnail_inspected']:raise ValueError('Full and thumbnail inspections required')
 if set(r['evidence'])!=set(IDS) or any(not x.strip() for x in r['evidence'].values()):raise ValueError('Concrete evidence required for every dimension')
 q=score(r['scores']);eligible=not r['blockers'] and r['class_pass'] and r['canon_pass'];folder=(run/p['request']).parent
 if 'allocation_policy' in s:
  require_gate(s)
  expected=next(c['background'] for c in s['plan']['variants'][0]['cards'] if c['id']==p['card'])
  if type(r['observed_category']) is not int or r['observed_category'] not in range(1,6) or not r['category_evidence'].strip():raise ValueError('Observed category and evidence required')
  eligible=eligible and r['observed_category']==expected
 entry={'iteration':p['iteration'],'image':(folder/'native.png').relative_to(run).as_posix(),'sha256':digest(folder/'native.png'),'quality':q,'eligible':eligible,**r}
 c=s['cards'][p['card']];c['history'].append(entry);old=c['history'][c['best']]
 if eligible and (not old['eligible'] or q>old['quality']):c['best']=len(c['history'])-1
 snapshot=saved_policy(s)
 entry['plateau_signal']=plateau_signal(c['history'],acceptance_policy=snapshot)
 c['plateau_signal']=entry['plateau_signal']
 c['status']=decide(c['history'],acceptance_policy=snapshot);c['next_feedback']=r['feedback'];c['next_strategy']=r['next_strategy']
 if c['status']=='revise' and (not r['feedback'].strip() or not r['next_strategy'].strip()):raise ValueError('Revision needs actionable feedback and strategy')
 write(folder/'review.json',entry);s['pending']=None;write(run/'state.json',s);return status(run)

def execute(args):
 if args.command=='refine-start':return start(args.run,args.source,args.audit,args.allocation)
 if args.command=='refine-next':return next_job(args.run)
 if args.command=='refine-attach':return attach(args.run,args.job,args.image,args.seconds)
 if args.command=='refine-review':return review(args.run,args.job,args.response)
 return status(args.run)
