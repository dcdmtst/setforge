"""Single-writer coordinator over independent, leased card jobs.

The legacy engine remains the authority for scored acceptance and immutable
inputs. Workers only exchange envelopes through WorkQueue; no provider is called
while the coordinator owns the run lock.
"""
import copy
from pathlib import Path

from .engine import Pipeline, NeedsInput
from .workqueue import WorkQueue
from .io import read, write, sha, event
from .quality import dependencies, pixel_digest, identity
from .allocation import require_gate, check as check_allocation

ACTIVE={'ready','leased','submitted','uncertain'}
CARD_KINDS={'image','card_review','entity_review'}


def load(run):
    return (ParallelPipeline if 'orchestration' in read(Path(run)/'state.json') else Pipeline)(run)


def _capacity(value,name):
    if isinstance(value,bool) or not isinstance(value,int) or value<1:
        raise ValueError(name+' must be a positive integer.')
    return value


def enable(pipeline, artists=2, reviewers=2, workers=None):
    _capacity(artists,'artists');_capacity(reviewers,'reviewers')
    if workers is not None:_capacity(workers,'workers')
    if 'orchestration' in pipeline.state:
        result=ParallelPipeline(pipeline.run)
        if result.state['orchestration'].get('version',1)>=2:
            result.set_capacity(artists=artists,reviewers=reviewers,workers=workers)
        else:
            if reviewers!=2 or workers is not None:
                raise ValueError('Historical orchestration keeps its original reviewer/worker policy.')
            result.set_artists(artists)
        return result
    if pipeline.state['jobs'] or pipeline.state['plan']:
        raise ValueError('Enable a queue on a new run; historical runs keep their original orchestration.')
    pipeline.state['orchestration']={'version':2,'artists':artists,'reviewers':reviewers,
                                     'workers':workers if workers is not None else artists+1,
                                     'workers_auto':workers is None,'card_blockers':{}}
    WorkQueue(pipeline.run).configure(artists=artists,reviewers=reviewers,
                                     workers=pipeline.state['orchestration']['workers'],managed_retries=True)
    pipeline.save();event(pipeline.run,'orchestration_enabled',artists=artists,reviewers=reviewers,
                         workers=pipeline.state['orchestration']['workers'],version=2)
    return ParallelPipeline(pipeline.run)


class ParallelPipeline(Pipeline):
    def __init__(self,run):
        super().__init__(run)
        if 'orchestration' not in self.state:raise ValueError('This run has no queue.')
        self.queue=WorkQueue(self.run)

    def set_artists(self,artists):
        self.set_capacity(artists=artists)

    def set_capacity(self,artists=None,reviewers=None,workers=None):
        orchestration=self.state['orchestration']
        for name,value in [('artists',artists),('reviewers',reviewers),('workers',workers)]:
            if value is not None:_capacity(value,name)
        if orchestration.get('version',1)==1:
            if reviewers is not None or workers is not None:
                raise ValueError('Historical orchestration keeps its original reviewer/worker policy.')
            if artists is not None:
                self.queue.configure(artists)
                orchestration['artists']=artists
                event(self.run,'artist_capacity_changed',artists=artists);self.save()
            return
        if artists is not None:
            orchestration['artists']=artists
            if workers is None and orchestration.get('workers_auto',False):
                orchestration['workers']=artists+1
        if reviewers is not None:orchestration['reviewers']=reviewers
        if workers is not None:orchestration.update(workers=workers,workers_auto=False)
        self.queue.configure(**self._queue_configuration())
        event(self.run,'worker_capacity_changed',artists=orchestration['artists'],
              reviewers=orchestration['reviewers'],workers=orchestration['workers']);self.save()

    def _queue_configuration(self):
        orchestration=self.state['orchestration']
        desired={'artists':orchestration['artists'],'managed_retries':True}
        if orchestration.get('version',1)>=2:
            desired.update(reviewers=orchestration['reviewers'],workers=orchestration['workers'])
        return desired

    def _scheduling_context(self):
        """Current unresolved identities and downstream work, never request edits."""
        if not self.quality_v2 or not self.state.get('plan'):
            return set(),{},set()
        plan=self.state['plan'];pinned={identity(e,e.get('variant_id')) for e in self.state['canon']}
        sources={e['source_card_id'] for v in plan['variants'] for e in v['entities']
                 if identity(e,v['id']) not in pinned}
        graph=dependencies(plan,self.state['canon'])
        original_graph=dependencies(plan,read(self.run/'inputs/canon.json'))
        pending=set(graph)-set(self.state['accepted'])-set(self.state.get('discarded_cards',{}))
        downstream={}
        for source in sources:
            closure={source}
            while True:
                expanded=closure|{key for key,deps in graph.items() if deps&closure}
                if expanded==closure:break
                closure=expanded
            downstream[source]=len((closure-{source})&pending)
        return sources,downstream,{key for key,deps in original_graph.items() if deps}

    def _job_priority(self,kind,card,context=None):
        if self.state['orchestration'].get('version',1)<2 or not card:return 0
        sources,downstream,consumers=context or self._scheduling_context()
        fanout=downstream.get(card,0)
        if kind=='entity_review':return 600+fanout if card in sources else 100
        if kind=='card_review':return 500+fanout if card in sources else (400 if card in consumers else 100)
        if kind=='image':return 300+fanout if card in sources else (200 if card in consumers else 0)
        return 0

    def _refresh_priorities(self,context):
        for job in self.queue.jobs():
            if job['status']!='ready':continue
            priority=self._job_priority(job['kind'],job.get('card'),context)
            if job.get('priority',0)!=priority:self.queue.set_priority(job['id'],priority)

    def _schedule_v2(self,cards,deps):
        context=self._scheduling_context()
        self._refresh_priorities(context)
        active=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
        occupied={j['card'] for j in active}
        capacities=self.state['orchestration']
        review_limit=capacities['artists']+capacities['reviewers']
        review_slots=review_limit-sum(j['kind'] in ('card_review','entity_review') for j in active)
        entity_targets=self.entity_review_targets()
        source_targets={key for key,_ in entity_targets}
        viable_sources={key for key in context[0]
                        if key not in self.state.get('discarded_cards',{})
                        and key not in capacities['card_blockers']
                        and (key in occupied or self._card_phase(key) is not None or key in source_targets)}
        # Do not fill every registered slot with props while a slow identity
        # image is still running. Reserve only useful critical slots; existing
        # jobs are never cancelled or reissued to implement this preference.
        reserve=min(capacities['reviewers'],len(viable_sources))
        ordinary_limit=review_limit-reserve
        ordinary_count=sum(j['kind'] in ('card_review','entity_review')
                           and self._job_priority(j['kind'],j['card'],context)<500 for j in active)
        reviews=[('entity_review',key,eid) for key,eid in entity_targets]
        reviews += [('card_review',key,None) for key in cards if self._card_phase(key)=='card_review']
        reviews.sort(key=lambda item:-self._job_priority(item[0],item[1],context))
        for kind,key,eid in reviews:
            if review_slots<=0:break
            if key in occupied:continue
            ordinary=self._job_priority(kind,key,context)<500
            if ordinary and ordinary_count>=ordinary_limit:continue
            try:self._issue(kind,key,eid)
            except NeedsInput:continue
            occupied.add(key);review_slots-=1
            if ordinary:ordinary_count+=1
        active=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
        occupied={j['card'] for j in active}
        slots=capacities['artists']-sum(j['kind']=='image' for j in active)
        backlog=sum(j['kind'] in ('card_review','entity_review') for j in active)
        # Deferred ordinary reviews count too: reserving critical queue slots
        # must not turn unregistered candidates into an unbounded hidden queue.
        backlog+=sum(key not in occupied and self._card_phase(key)=='card_review' for key in cards)
        backlog+=sum(key not in occupied for key,_ in self.entity_review_targets())
        if backlog>=review_limit:return
        images=sorted(cards,key=lambda key:-self._job_priority('image',key,context))
        for key in images:
            if slots<=0:break
            if key in occupied or self._card_phase(key)!='image':continue
            if ((self.separate_entities and deps.get(key))
                    or (not self.separate_entities and deps.get(key,set())-set(self.state['accepted']))):continue
            try:self._issue('image',key)
            except NeedsInput:continue
            occupied.add(key);slots-=1

    def block(self,reason):
        context=self.state.get('_queue_applying') or self.state.get('_queue_issuing')
        if context and context.get('card'):
            key=context['card']
            self.state['orchestration']['card_blockers'][key]={'phase':self.state['phase'],'reason':reason}
            event(self.run,'card_blocked',card=key,reason=reason)
        else:
            self.state['orchestration']['global_blocker']=reason
        super().block(reason)

    def accept(self,*args,**kwargs):
        raise ValueError('Queued runs use claim/deliver; only the coordinator applies results.')

    def validate_plan(self,plan):
        super().validate_plan(plan)
        if not self.quality_v2:
            pinned={identity(e,e.get('variant_id')) for e in self.state['canon']}
            if any(identity(e,v['id']) not in pinned for v in plan['variants'] for e in v['entities']):
                raise ValueError('Parallel new canon needs explicit source_card_id; use project schema_version 2. Historical serial runs remain unchanged.')

    def import_image(self,response,job):
        # Two unreviewed siblings must not receive identical pixels either.
        from pathlib import Path
        source=Path(response['image_path'])
        if not source.is_absolute():source=self.run/source
        if source.is_file():
            digest=pixel_digest(source)
            siblings=[a for key,a in self.state['latest'].items() if key!=self.state['current']]
            if any(pixel_digest(self.run/a['path'])==digest for a in siblings):
                raise ValueError('Image duplicates an unreviewed sibling candidate.')
        return super().import_image(response,job)

    def apply_review(self,review,phase):
        affected=set(review.get('affected_cards',[]))
        graph={}
        identity_invalidated=(not self.separate_entities or any(f.get('criterion')=='canon' and f.get('severity')=='blocking' for f in review.get('findings',[])))
        if phase=='set_review' and review['action']=='revise' and self.quality_v2 and identity_invalidated:
            graph=dependencies(self.state['plan'],read(self.run/'inputs/canon.json'))
        super().apply_review(review,phase)
        if graph:
            closure=set(affected)
            while True:
                expanded=closure|{key for key,deps in graph.items() if deps&closure}
                if expanded==closure:break
                closure=expanded
            removed=[e for e in self.state['canon'] if e.get('established_by') in closure]
            if removed:
                self.state['orchestration'].setdefault('invalidated_canon',[]).extend(copy.deepcopy(removed))
                self.state['canon']=[e for e in self.state['canon'] if e not in removed]
                write(self.run/'canon.json',self.state['canon'])
                for key in closure:
                    self.state['accepted'].pop(key,None)
                    self.state['art_stops'].pop(key,None)
                    invalid=self.state['art_invalidated']
                    for entry in self.state['art_history'].get(key,[]):
                        if entry['artifact']['job_id'] not in invalid:invalid.append(entry['artifact']['job_id'])
                    if key not in affected:
                        self.state['feedback']['image:'+key]={'summary':'Canon source was invalidated by whole-set review; wait for and preserve the accepted replacement identity.'}
                event(self.run,'canon_dependents_invalidated',sources=sorted(affected),cards=sorted(closure))

    def freeze_canon(self,key):
        before={(e['scope'],e['id'],e.get('variant_id')) for e in self.state['canon']}
        super().freeze_canon(key)
        old=self.state['orchestration'].get('invalidated_canon',[])
        for entity in self.state['canon']:
            token=(entity['scope'],entity['id'],entity.get('variant_id'))
            if token in before:continue
            prior=[e.get('version',1) for e in old if (e['scope'],e['id'],e.get('variant_id'))==token]
            if prior:
                entity['version']=max(prior)+1
                event(self.run,'canon_version_replaced',entity=entity['id'],version=entity['version'],source=key)
        write(self.run/'canon.json',self.state['canon'])

    def _register_pending(self):
        pending=self.state['pending']
        if pending:
            request=read(self.run/'jobs'/pending['id']/'request.json')
            card=self.state['current'] if request['kind'] in CARD_KINDS else None
            options={'priority':self._job_priority(request['kind'],card)} if self.state['orchestration'].get('version',1)>=2 else {}
            self.queue.register(pending,card=card,kind=request['kind'],**options)
            if card:
                self.state.update(pending=None,current=None,phase='parallel_art',status='running',reason='')
                self.state.pop('_queue_issuing',None);self.save()

    def _finish_applied(self,job):
        # State commit precedes inbox acknowledgement. Replaying this projection
        # is harmless after a crash; the scored judgement itself is never replayed.
        if not self.state['plan'] and job['kind']=='card_review':
            self._cancel_old_plan(job['id'])
        elif job.get('card'):
            self.state.update(pending=None,current=None,phase='parallel_art',status='running',reason='')
        self.state.pop('_queue_applying',None)
        self.save()

    def _cancel_old_plan(self,except_id):
        for other in self.queue.jobs():
            if other['id']!=except_id and other['status'] in ACTIVE:
                self.queue.cancel(other['id'],'Plan invalidated; original job remains in history.')
                record=next(j for j in self.state['jobs'] if j['id']==other['id'])
                record.update(status='cancelled',reason='Plan invalidated')
        self.state['orchestration']['card_blockers']={}
        self.state.update(pending=None,current=None,phase='plan',status='running',reason='')

    def _consume(self):
        for job in self.queue.inbox():
            record=next(j for j in self.state['jobs'] if j['id']==job['id'])
            if record['status'] in ('received','rejected','error'):
                if self.state.get('_queue_applying',{}).get('id')==job['id']:
                    self._finish_applied(job)
                self.queue.acknowledge(job['id'],'error' if record['status']=='error' else 'done')
                continue
            if record['status']=='cancelled':
                self.queue.cancel(job['id'],'Plan invalidated');continue
            self.state.update(phase=job['kind'],current=job['card'],status='running',reason='',
                              pending={k:job[k] for k in ('id','role','task_key','request_sha256')})
            self.state['_queue_applying']={'id':job['id'],'kind':job['kind'],'card':job['card']}
            if job['kind']=='entity_review':
                request=read(self.run/'jobs'/job['id']/'request.json')
                self.state['current_entity']=request['context']['entity']['id']
            self.save()
            if sha(self.run/'jobs'/job['id']/'request.json')!=job['request_sha256']:
                # Integrity errors hold the task; never silently issue a new call.
                self.state['orchestration']['global_blocker']='Queue request changed: '+job['id']
                self.save();raise ValueError(self.state['orchestration']['global_blocker'])
            try:
                if job['error']:
                    super().provider_failure(ValueError(job['error']))
                else:
                    if job['kind']=='card_review':
                        request=read(self.run/'jobs'/job['id']/'request.json')
                        expected=self.state['latest'][job['card']]['path']
                        candidates=[i['path'] for i in request['images'] if i['role'].startswith('candidate')]
                        if candidates and expected not in candidates:
                            raise ValueError('Review refers to an obsolete candidate.')
                    super().accept(job['response'],executor=job['worker'],model=job['model'] or 'unspecified')
            except NeedsInput:
                # block() has recorded a local or global hold before saving.
                pass
            except (ValueError,OSError,KeyError,StopIteration) as exc:
                if self.state['pending']:
                    super().provider_failure(exc)
                else:raise
            self._finish_applied(job)
            record=next(j for j in self.state['jobs'] if j['id']==job['id'])
            self.queue.acknowledge(job['id'],'error' if record['status']=='error' else 'done')

    def _expired_reasoning(self):
        # A fresh reasoning invocation is a fresh charged/counted attempt, not
        # an unlimited re-lease hidden under the original job's reservation.
        for job in self.queue.jobs():
            if job['status']!='retry_wait':continue
            record=next(j for j in self.state['jobs'] if j['id']==job['id'])
            if record['status'] not in ('error','cancelled'):
                record.update(status='error',reason='Reasoning lease expired; replacement must reserve a new attempt.')
                self.state['feedback'][job['task_key']]=record['reason']
                if not job['card']:
                    self.state.update(phase=job['kind'],pending=None,current=None)
                event(self.run,'reasoning_lease_expired',id=job['id'],card=job['card'])
                self.save()
            self.queue.cancel(job['id'],'Expired reasoning attempt recorded; retry uses fresh budget reservation.')

    def _issue(self,phase,card=None,entity_id=None):
        self.state.update(phase=phase,current=card,pending=None,status='running',reason='')
        if entity_id:self.state['current_entity']=entity_id
        self.state['_queue_issuing']={'kind':phase,'card':card}
        try:
            super().issue()
        except NeedsInput:
            if card:
                self.state.update(phase='parallel_art',current=None,pending=None,status='running',reason='')
            self.state.pop('_queue_issuing',None);self.save()
            raise
        self._register_pending()
        self.state.pop('_queue_issuing',None);self.save()

    def _card_phase(self,key):
        if key in self.state['accepted'] or key in self.state.get('discarded_cards',{}) or key in self.state['orchestration']['card_blockers']:return None
        if self.state['art_stops'].get(key) in ('plateau','iteration_limit','discarded','replacement_pending'):return None
        latest=self.state['latest'].get(key)
        if latest and not any(h['artifact']['job_id']==latest['job_id'] for h in self.state['art_history'].get(key,[])):
            return 'card_review'
        return 'image'

    def _wait(self):
        jobs=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
        uncertain=[j['id'] for j in jobs if j['status']=='uncertain']
        blockers=self.state['orchestration']['card_blockers']
        if jobs:
            self.state.update(status='waiting_for_workers',reason='Waiting for queued workers'+(' or image reconciliation: '+', '.join(uncertain) if uncertain else '.'))
        else:
            stopped=any(v['phase']=='art_stopped' for v in blockers.values())
            self.state.update(status='needs_attention',phase='art_stopped' if stopped else self.state['phase'],
                              reason=self.state['orchestration'].get('global_blocker') or ('Unaccepted cards: '+', '.join(blockers) if blockers else 'No eligible jobs; inspect canon dependencies and limits.'))
        self.save();raise NeedsInput(self.state['reason'])

    def advance(self):
        self.check_snapshot()
        desired=self._queue_configuration()
        if self.queue.configuration()!=desired:
            self.queue.configure(**desired)
        self._expired_reasoning()
        # Recover an issue persisted before registration or a consume persisted
        # before acknowledgement. No worker invocation occurs here.
        applying=self.state.get('_queue_applying')
        if applying:
            job=next(j for j in self.queue.jobs() if j['id']==applying['id'])
            record=next(j for j in self.state['jobs'] if j['id']==job['id'])
            if record['status'] in ('received','rejected','error'):
                self._finish_applied(job)
        if not applying:self._register_pending()
        self._consume()
        if self.state['status'] in ('complete','completed_with_rejections'):return
        if self.state['orchestration'].get('global_blocker'):
            self._wait()
        while True:
            phase=self.state['phase']
            if phase=='allocation_check':
                self.validate_plan(self.state['plan'])
                gate=check_allocation(self.state['plan'],self.state['allocation_policy'])
                write(self.run/'allocation.json',gate)
                self.state['allocation_gate']={k:gate[k] for k in ('verdict','plan_sha256')}
                self.state['allocation_gate']['file_sha256']=sha(self.run/'allocation.json')
                event(self.run,'gate_result',gate='pre_art_allocation',verdict='pass',plan_sha256=gate['plan_sha256'])
                self.state['phase']='plan_selection' if self.needs_plan_selection() else 'parallel_art';self.save();continue
            if phase=='plan_selection':
                self.state.update(status='waiting_for_selection',reason='Choose one reviewed composition in explicit pilot mode.');self.save();raise NeedsInput(self.state['reason'])
            if phase in ('next_card','image','card_review','entity_review','art_stopped'):
                self.state.update(phase='parallel_art',current=None,pending=None);phase='parallel_art'
            if phase=='parallel_art':
                if self.state.get('replacement_pending') and not any(j['status'] in ACTIVE for j in self.queue.jobs()):
                    self.state.update(phase='replacement_plan', replacement_current=next(iter(self.state['replacement_pending'])), current=None)
                    self.save();continue
                require_gate(self.state)
                if sha(self.run/'allocation.json')!=self.state['allocation_gate']['file_sha256']:
                    raise ValueError('Approved allocation artifact changed.')
                active=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
                occupied={j['card'] for j in active}
                deps=dependencies(self.state['plan'],self.state['canon']) if self.quality_v2 else {}
                cards=[k for _,_,k in self.cards()]
                if self.state['orchestration'].get('version',1)>=2:
                    self._schedule_v2(cards,deps)
                else:
                    # Historical queues retain plan ordering and their original
                    # single-reviewer backlog behavior without migration.
                    for key,eid in self.entity_review_targets():
                        if key in occupied:continue
                        try:self._issue('entity_review',key,eid)
                        except NeedsInput:pass
                        occupied.add(key)
                    for key in cards:
                        if key not in occupied and self._card_phase(key)=='card_review':
                            try:self._issue('card_review',key)
                            except NeedsInput:pass
                            occupied.add(key)
                    active=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
                    occupied={j['card'] for j in active}
                    slots=self.state['orchestration']['artists']-sum(j['kind']=='image' for j in active)
                    backlog=sum(j['kind']=='card_review' for j in active)
                    if backlog<=self.state['orchestration']['artists']:
                        for key in cards:
                            if slots<=0:break
                            if key in occupied or self._card_phase(key)!='image':continue
                            if ((self.separate_entities and deps.get(key))
                                    or (not self.separate_entities and deps.get(key,set())-set(self.state['accepted']))):continue
                            try:self._issue('image',key);slots-=1
                            except NeedsInput:pass
                active=[j for j in self.queue.jobs() if j['status'] in ACTIVE]
                if self.state.get('replacement_pending') and not active:
                    self.state.update(phase='replacement_plan', replacement_current=next(iter(self.state['replacement_pending'])), current=None)
                    self.save();continue
                if all(k in self.state['accepted'] for k in cards) and not active:
                    self.state.update(phase='set_review',status='running',current=None,pending=None);self.save();continue
                if self.separate_entities and not active:
                    # All independent work was attempted. Unavailable identity
                    # sources cannot turn never-generated consumers into passes.
                    unresolved=[k for k in cards if k not in self.state['accepted'] and k not in self.state.get('discarded_cards',{})]
                    for key in unresolved:
                        if deps.get(key) and self._card_phase(key)=='image':
                            self.discard_card(key,'Required identity could not be qualified; no eligible source remains.')
                            if self.state['art_stops'].get(key) != 'replacement_pending':self.state['art_stops'][key]='discarded'
                    if self.state.get('replacement_pending'):
                        self.state.update(phase='replacement_plan',replacement_current=next(iter(self.state['replacement_pending'])),current=None)
                        self.save();continue
                    if all(k in self.state['accepted'] or k in self.state.get('discarded_cards',{}) for k in cards):
                        complete=[v['id'] for v in self.state['plan']['variants']
                                  if all(v['id']+'/'+c['id'] in self.state['accepted'] for c in v['cards'])]
                        self.state['review_variant_ids']=complete
                        if complete:
                            self.state.update(phase='set_review',status='running',current=None,pending=None);self.save();continue
                        self.state.update(status='completed_with_rejections',phase='discarded',reason='No complete ten-card set passed; discarded sets retained in history.')
                        self.save();return
                self.state.update(phase='parallel_art',current=None,pending=None)
                self._wait()
            if phase=='export':
                # Legacy export is deterministic and makes no provider calls.
                return super().advance()
            active=[j for j in self.queue.jobs() if j['status'] in ACTIVE and j['card'] is None]
            if not active:
                try:self._issue(phase)
                except NeedsInput:pass
            self._wait()

    def resume_attention(self,note,extensions=None):
        if any(self.state['art_stops'].get(k) in ('plateau','iteration_limit') for k in self.state['orchestration']['card_blockers']):
            raise ValueError('Stopped art is not accepted; continue cannot reset plateau or five edits.')
        super().resume_attention(note,extensions)
        self.state['orchestration'].pop('global_blocker',None)
        for key,block in self.state['orchestration']['card_blockers'].items():
            self.state['feedback'][block['phase']+':'+key]=note
        self.state['orchestration']['card_blockers']={}
        self.save()
