"""Producer delivery and CI manifest. Never changes acceptance or generates art."""
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from .io import inside,read,sha,write,locked
from .acceptance import score
from .nameplate import preflight,DEFAULT_POLICY
from .ui_skin import measure_label
from .naming import names,display_title


def presentation_fingerprint():
    """Invalidate cached ZIPs when embedded UI changes, without touching art."""
    from importlib.resources import files
    package=files('setforge')
    return hashlib.sha256(b''.join(package.joinpath(name).read_bytes() for name in
        ('showcase.py','ui_skin.py','neutral_skin.py','rating_ui.py','defaults/ui/rating-stars.json'))).hexdigest()


def result_manifest(run):
    run=Path(run).resolve();state=read(run/'state.json');brief=read(run/'inputs/brief.json')
    expected=brief.get('variants',3)*10;groups=[];valid=0;errors=[]
    threshold=max(93.5,state.get('art_acceptance',{}).get('threshold',93.5))
    exports={a['key']:a for a in read(run/'exports/manifest.json')} if (run/'exports/manifest.json').exists() else {}
    mapping=names(run);shown=copy.deepcopy(state.get('plan') or {'variants':[]})
    for variant in shown['variants']:
        variant['title']=display_title(mapping,variant)
        for card in variant['cards']:card['title']=display_title(mapping,variant,card)
    notes=read(run/'presentation-naming-notes.json') if (run/'presentation-naming-notes.json').exists() else {}
    title_check=preflight(shown,{**DEFAULT_POLICY,**state.get('nameplate_policy',{}),
                                'producer_flagged_labels':notes.get('producer_flagged_labels',[])},measure_label)
    if title_check['verdict']!='pass':errors.append({'gate':'nameplate','failed_labels':title_check['failed_labels']})
    if state.get('discarded_cards') or state.get('discarded_sets'):errors.append({'gate':'discarded_content'})
    for variant in (state.get('plan') or {}).get('variants',[]):
        cards=[]
        for index,card in enumerate(variant['cards'],1):
            key=variant['id']+'/'+card['id'];artifact=state.get('accepted',{}).get(key)
            ready=bool(artifact and artifact.get('eligible') and artifact.get('quality',0)>=threshold)
            entry={'id':card['id'],'title':display_title(mapping,variant,card),'stars':card.get('stars'),
                   'golden':card.get('golden',False),'category':card['background'],
                   'accepted':False,'quality':artifact.get('quality') if artifact else None}
            if ready:
                try:
                    if score(artifact.get('scores',{}))!=artifact['quality'] or state.get('art_stops',{}).get(key)!='accepted':
                        raise ValueError('Scored acceptance record is inconsistent.')
                    if artifact.get('stars')!=card.get('stars') or artifact.get('golden')!=card.get('golden'):
                        raise ValueError('Accepted stars/gold differ from allocation.')
                    native=inside(run,artifact['path']);export=exports[key];sized=inside(run,export['path'])
                    if sha(native)!=artifact['sha256'] or sha(sized)!=export['sha256']:
                        raise ValueError('Artifact content hash changed.')
                    if export['source_sha256']!=artifact['sha256'] or export.get('acceptance')!='accepted':
                        raise ValueError('Export does not bind the accepted native.')
                    if artifact.get('observed_category')!=card['background']:
                        raise ValueError('Observed category differs from allocation.')
                    from .construction import applies, validate_card
                    if applies(state,key):
                        history=next(r for r in reversed(state['art_history'][key]) if r['artifact']['job_id']==artifact['job_id'])
                        visual_review=read(inside(run,'jobs/'+history['review_job']+'/response.json'))
                        if not validate_card(run,visual_review,artifact,key):
                            raise ValueError('Construction is not accepted; score cannot override it.')
                    entry.update(accepted=True,native_path=f"images/{variant['id']}/{index:02d}-{card['id']}.png",
                                 path=f"cards/{variant['id']}/{index:02d}-{card['id']}.png",
                                 source_sha256=artifact['sha256'],sha256=export['sha256'],
                                 _native_source=artifact['path'],_export_source=export['path'])
                    valid+=1
                except (OSError,ValueError,KeyError,StopIteration) as exc:errors.append({'card':key,'error':str(exc)})
            cards.append(entry)
        groups.append({'id':variant['id'],'title':display_title(mapping,variant),'cards':cards})
    if state['status']=='complete' and state.get('construction_policy'):
        try:
            from .construction import targets,validate_set
            final=next(j for j in reversed(state['jobs']) if j['kind']=='set_review' and j.get('status')=='received')
            validate_set(run,read(inside(run,'jobs/'+final['id']+'/response.json')),targets(state))
        except (OSError,ValueError,KeyError,StopIteration) as exc:
            errors.append({'gate':'final_construction','error':str(exc)})
    ready=(state['status']=='complete' and len(groups)==brief.get('variants',3)
           and all(len(g['cards'])==10 for g in groups) and valid==expected and not errors)
    return {'schema_version':1,'run_id':run.name,'status':state['status'],
            'full_delivery_ready':ready,'requested_variants':brief.get('variants',3),
            'expected_cards':expected,'accepted_cards':valid,'threshold':threshold,
            'theme':brief['theme'],'wishes':brief.get('wishes',''),'variants':groups,
            'errors':errors,'artifacts':{'report':'report.html','archive':'delivery.zip'}}


def public_manifest(manifest):
    value=copy.deepcopy(manifest)
    for group in value['variants']:
        for card in group['cards']:
            card.pop('_native_source',None);card.pop('_export_source',None)
    return value


def save_manifest(run):
    with locked(run):
        value=public_manifest(result_manifest(run));write(Path(run)/'result-manifest.json',value)
    return value


def export_bundle(run,out=None):
    """Final assets only. Full private history remains a separate explicit `pack`."""
    run=Path(run).resolve();out=Path(out).resolve() if out else run/'delivery.zip'
    if out.suffix.lower()!='.zip':raise ValueError('Delivery output must be a .zip file.')
    if out.is_relative_to(run) and out.parent!=run:raise ValueError('Write delivery beside the report or outside the run, never over production inputs/artifacts.')
    with locked(run):
        value=result_manifest(run);public=public_manifest(value);write(run/'result-manifest.json',public)
        if not value['full_delivery_ready']:
            raise ValueError('Full delivery is not accepted. Read result-manifest.json; no accepted ZIP was produced.')
        fingerprint=hashlib.sha256((json.dumps(public,sort_keys=True)+presentation_fingerprint()).encode()).hexdigest()
        receipt=run/'delivery-receipt.json'
        if out.exists() and receipt.exists():
            previous=read(receipt)
            if previous.get('fingerprint')==fingerprint and previous.get('path')==str(out) and previous.get('sha256')==sha(out):
                return previous
        elif out.exists():raise ValueError('Output already exists without a matching delivery receipt; choose another filename.')
        out.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='setforge-delivery-') as folder:
            stage=Path(folder);state=read(run/'state.json');brief=read(run/'inputs/brief.json')
            display={'status':'complete','plan':copy.deepcopy(state['plan']),'accepted':{},'latest':{},
                     'canon':[],'jobs':[],'art_acceptance':state.get('art_acceptance'),
                     'production_contract':state.get('production_contract',{}),'delivery_view':True}
            if (run/'display-titles.json').exists():shutil.copy2(run/'display-titles.json',stage/'display-titles.json')
            export_rows=[]
            for group in value['variants']:
                for card in group['cards']:
                    for source,destination in [(card['_native_source'],card['native_path']),(card['_export_source'],card['path'])]:
                        target=inside(stage,destination);target.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copy2(inside(run,source),target)
                    key=group['id']+'/'+card['id'];artifact=copy.deepcopy(state['accepted'][key])
                    artifact['path']=card['native_path'];display['accepted'][key]=artifact
                    export_rows.append({'key':key,'path':card['path'],'sha256':card['sha256']})
            write(stage/'result-manifest.json',public);write(stage/'exports/manifest.json',export_rows)
            display['producer_revision_summaries'] = []
            for amendment in state.get('producer_revisions', []):
                entry = read(inside(run,amendment['path']))
                display['producer_revision_summaries'].append({'card':entry['card'], 'note':entry['note'],
                    'previous_quality':entry['previous_artifact'].get('quality')})
                current=state['accepted'].get(entry['card'],{})
                audit=next((r.get('construction_review') for r in reversed(state.get('art_history',{}).get(entry['card'],[]))
                            if r['artifact']['job_id']==current.get('job_id')),None)
                if audit:display['producer_revision_summaries'][-1]['construction_review']=audit
            from .showcase import render
            from .timing import collect_timing,render_timing
            render(stage,display,brief,audit_path=None)
            timing=collect_timing(run,state);write(stage/'timing-data.json',timing)
            timing_html=render_timing(run,state)
            # No private job requests or source references in the producer bundle.
            timing_html=re.sub(r'href="jobs/[^\"]+"','href="#sf-time-analysis"',timing_html)
            page=(stage/'report.html').read_text(encoding='utf8')
            empty='<section class="sf-time"><h2>Время работы</h2><p>Нет записанных временных событий.</p></section>'
            page=page.replace(empty,timing_html)
            (stage/'report.html').write_text(page,encoding='utf8')
            (stage/'START-HERE.txt').write_text('Open report.html. cards/ contains game-size PNGs; images/ contains accepted native PNGs. result-manifest.json is the CI contract. No source references or rejected images are included.\n',encoding='utf8')
            temporary=out.with_name(out.name+'.tmp')
            try:
                with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
                    for file in sorted(stage.rglob('*')):
                        if file.is_file():z.write(file,file.relative_to(stage).as_posix())
                os.replace(temporary,out)
            finally:
                if temporary.exists():temporary.unlink()
        record={'path':str(out),'sha256':sha(out),'bytes':out.stat().st_size,
                'fingerprint':fingerprint,'accepted_cards':value['accepted_cards'],'full_delivery_ready':True}
        write(receipt,record)
        return record
