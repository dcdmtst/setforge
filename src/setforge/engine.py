"""Persisted production state machine; only this module advances gates."""

import copy
import hashlib
import json
import shutil
from importlib.resources import files
from pathlib import Path

from PIL import Image

from . import __version__
from .io import event, inside, now, read, sha, write
from .project import snapshot
from .providers import invoke
from .schemas import BRIEF, IMAGE, PLAN, REVIEW, validate
from .schemas import contracts
from .quality import check_concepts, check_findings, dependencies, pixel_digest
from .acceptance import policy as art_policy, score as art_score, decide as art_decide, plateau_signal, MAX_EDITS
from .schemas import scored_card_review
from .schemas import production_plan, ENTITY_REVIEW
from .allocation import policy as allocation_policy, check as check_allocation, require_gate

CRITERIA = {
    "brief_review": ["clarity", "conflicts"],
    "plan_review": ["theme", "required_constraints", "backgrounds", "semantic_diversity", "variant_differences", "canon"],
    "card_review": ["brief_match", "style", "composition", "background", "materials", "canon", "no_game_ui"],
    "set_review": ["coherence", "style_consistency", "semantic_diversity", "visual_diversity", "variant_differences", "canon", "required_constraints"],
}


class NeedsInput(Exception):
    pass


class Pipeline:
    def __init__(self, run):
        self.run = Path(run).resolve()
        self.state = read(self.run / "state.json")
        self.config = read(self.run / "inputs/project.json")
        self.quality_v2 = self.config.get("schema_version", 1) == 2
        self.plan_schema, self.review_schema = contracts(self.config.get("schema_version", 1))
        self.scored_art = 'art_acceptance' in self.state
        self.allocated = 'allocation_policy' in self.state
        if self.allocated:
            self.plan_schema = production_plan(self.config.get('schema_version', 1), wishes_response='production_contract' in self.state)
        for name, extra in self.state.get("limit_extensions", {}).items():
            self.config["limits"][name] += extra
        self.brief = read(self.run / "inputs/brief.json")
        self.refs = read(self.run / "inputs/references.json")
        self.style = (self.run / "inputs/style.md").read_text(encoding="utf-8")

    @classmethod
    def create(cls, project, brief, run, artists=None, reviewers=2, workers=None):
        brief = {"wishes": "", "required": [], "forbidden": [], "variants": 3, **brief}
        validate(brief, BRIEF)
        if not brief['theme'].strip():raise ValueError('Theme is required.')
        run = Path(run).resolve()
        if run.exists():
            raise ValueError("Run already exists. Resume it or choose a new folder.")
        run.mkdir(parents=True)
        try:
            config, canon = snapshot(project, run)
            write(run / "inputs/brief.json", brief)
            write(run / "inputs/acceptance-policy.json", art_policy())
            allocation = allocation_policy(config, brief)
            write(run / 'inputs/allocation-policy.json', allocation)
            # The brief participates in the immutable input manifest as well.
            manifest = read(run / "inputs/manifest.json")
            manifest.append({"path": "inputs/brief.json", "sha256": sha(run / "inputs/brief.json")})
            manifest.append({'path': 'inputs/acceptance-policy.json',
                             'sha256': sha(run / 'inputs/acceptance-policy.json')})
            manifest.append({'path': 'inputs/allocation-policy.json',
                             'sha256': sha(run / 'inputs/allocation-policy.json')})
            write(run / "inputs/manifest.json", manifest)
            state = {"schema_version": 1, "version": __version__, "created": now(),
                     "status": "running", "phase": "brief_review", "pending": None,
                     "calls": 0, "estimated_cost": 0.0, "task_attempts": {},
                     "plan": None, "accepted": {}, "latest": {}, "current": None,
                     "feedback": {}, "canon": canon, "set_revisions": 0,
                     "jobs": [], "reason": "", "input_manifest_sha256": sha(run / "inputs/manifest.json")}
            state.update(art_acceptance=art_policy(), art_history={}, art_stops={}, art_invalidated=[])
            state.update(allocation_policy=allocation, allocation_gate=None)
            state['production_contract'] = {'mode': brief.get('production_mode', 'full'),
                                            'requested_variants': brief['variants'], 'cards_per_variant': 10}
            if config['rules'].get('title_preflight',True):
                from .nameplate import DEFAULT_POLICY
                state['nameplate_policy']=copy.deepcopy(DEFAULT_POLICY)
                write(run/'inputs/nameplate-policy.json',state['nameplate_policy'])
                manifest=read(run/'inputs/manifest.json')
                manifest.append({'path':'inputs/nameplate-policy.json','sha256':sha(run/'inputs/nameplate-policy.json')})
                write(run/'inputs/manifest.json',manifest)
                state['input_manifest_sha256']=sha(run/'inputs/manifest.json')
            code_files = [{"path": p.relative_to(Path(__file__).parent).as_posix(), "sha256": sha(p)}
                          for p in sorted(Path(__file__).parent.rglob("*"))
                          if p.is_file() and p.suffix in {".py", ".md", ".json", ".txt"}]
            write(run / "code-manifest.json", code_files)
            state["code_sha256"] = hashlib.sha256(json.dumps(code_files, sort_keys=True).encode()).hexdigest()
            write(run / "state.json", state)
            event(run, "created", version=__version__)
        except Exception:
            # Keep partial files available for diagnosis, but never label them a run.
            write(run / "initialization-error.json", {"error": "Input snapshot failed", "time": now()})
            raise
        pipeline = cls(run)
        if artists is not None:
            from .parallel import enable
            return enable(pipeline, artists=artists,reviewers=reviewers,workers=workers)
        return pipeline

    def save(self):
        write(self.run / "state.json", self.state)

    def block(self, reason):
        self.state.update(status="needs_attention", reason=reason)
        event(self.run, "needs_attention", reason=reason)
        self.save()
        raise NeedsInput(reason)

    def check_snapshot(self):
        if 'nameplate_policy' in self.state and self.state['nameplate_policy']!=read(self.run/'inputs/nameplate-policy.json'):
            raise ValueError('Nameplate policy differs from its immutable snapshot.')
        if self.allocated and self.state['allocation_policy'] != read(self.run / 'inputs/allocation-policy.json'):
            raise ValueError('Allocation policy differs from its immutable snapshot.')
        if self.allocated and self.state['phase'] in ('image','card_review','set_review','next_card','export','complete','plan_selection'):
            require_gate(self.state)
        if self.scored_art:
            effective = read(self.run / 'inputs/acceptance-policy.json')
            for amendment in self.state.get('policy_amendments', []):
                path = inside(self.run, amendment['path'])
                if sha(path) != amendment['sha256']:
                    raise ValueError('Policy amendment changed.')
                value = read(path)
                if value['before'] != effective:
                    raise ValueError('Policy amendment chain is broken.')
                effective = value['after']
            if self.state['art_acceptance'] != effective:
                raise ValueError('Art acceptance policy differs from its immutable snapshot or amendment chain.')
        if sha(self.run / "inputs/manifest.json") != self.state["input_manifest_sha256"]:
            raise ValueError("Input manifest changed. Start a new run for changed inputs.")
        for entry in read(self.run / "inputs/manifest.json"):
            if sha(inside(self.run, entry["path"])) != entry["sha256"]:
                raise ValueError(f"Input snapshot changed: {entry['path']}. Start a new run.")
        for key, artifact in self.state["accepted"].items():
            if sha(inside(self.run, artifact["path"])) != artifact["sha256"]:
                raise ValueError(f"Accepted artifact changed: {key}")
        for entity in self.state['canon']:
            source = entity.get('identity_source_artifact')
            if source and sha(inside(self.run, source['path'])) != source['sha256']:
                raise ValueError('Qualified identity source changed: ' + entity['id'])

    @property
    def separate_entities(self):
        return self.state.get('art_acceptance', {}).get('version', 1) >= 2

    def amend_policy(self, note):
        """Explicit, auditable policy migration; never resets image attempts."""
        self.check_snapshot()
        if not note.strip():raise ValueError('Policy amendment needs an explanation.')
        if self.state['status'] == 'complete':raise ValueError('Completed runs remain immutable.')
        if self.state.get('pending'):raise ValueError('Finish pending work before amending policy.')
        if 'orchestration' in self.state:
            from .workqueue import WorkQueue
            if any(j['status'] in ('ready','leased','submitted','uncertain') for j in WorkQueue(self.run).jobs()):
                raise ValueError('Finish or reconcile active queued jobs before amending policy.')
        before = copy.deepcopy(self.state['art_acceptance']); after = art_policy()
        if before == after:raise ValueError('This run already uses the current policy.')
        number = len(self.state.get('policy_amendments', [])) + 1
        path = self.run/'amendments'/f'{number:03d}-art-policy.json'
        write(path, {'time': now(), 'note': note, 'before': before, 'after': after,
                     'history_preserved': True, 'calls': self.state['calls'],
                     'task_attempts': copy.deepcopy(self.state['task_attempts'])})
        self.state.setdefault('policy_amendments', []).append({'path': path.relative_to(self.run).as_posix(), 'sha256': sha(path)})
        self.state['art_acceptance'] = after
        for key in self.state['art_history']:
            if key in self.state['accepted']:continue
            decision = art_decide(self.art_history(key), acceptance_policy=after)
            self.state['art_stops'][key] = decision
            if decision == 'discarded':self.discard_card(key, 'Three edits exhausted without acceptance.')
            else:
                self.state.get('orchestration', {}).get('card_blockers', {}).pop(key, None)
                history=self.state['art_history'][key]
                last=read(self.run/'jobs'/history[-1]['review_job']/'response.json')
                self.state['feedback']['image:'+key] = last
        self.state.update(status='running', phase='parallel_art' if 'orchestration' in self.state else 'next_card', reason='', current=None)
        event(self.run, 'policy_amended', amendment=path.relative_to(self.run).as_posix(), note=note)
        self.save()

    def discard_card(self, key, reason):
        self.state['accepted'].pop(key, None)
        self.state.setdefault('discarded_cards', {})[key] = {'reason': reason, 'time': now()}
        variant_id=key.split('/')[0]
        self.state.setdefault('discarded_sets', {})[variant_id] = {'reason': 'A required card was discarded; this is not a ten-card set.', 'cards': sorted(k for k in self.state['discarded_cards'] if k.startswith(variant_id+'/'))}
        self.state.get('orchestration', {}).get('card_blockers', {}).pop(key, None)
        event(self.run, 'card_discarded', card=key, reason=reason)

    def cards(self):
        if not self.state["plan"]:
            return []
        selected = self.production_ids()
        return [(v, c, f"{v['id']}/{c['id']}") for v in self.state["plan"]["variants"]
                if not selected or v["id"] in selected for c in v["cards"]]

    def production_ids(self):
        if self.state.get('production_contract', {}).get('mode') == 'full':
            return None
        return self.state.get('production_variant_ids')

    def needs_plan_selection(self):
        contract = self.state.get('production_contract')
        return contract['mode'] == 'pilot' if contract else self.config['rules'].get('select_before_art', False)

    def choose_plan(self, variant_id, note, executor):
        self.check_snapshot()
        if self.state.get('production_contract', {}).get('mode') == 'full':
            raise ValueError('All requested variants must be produced; choose-plan is available only after explicit pilot mode selection.')
        if self.state["phase"] != "plan_selection" or self.state["pending"]:
            raise ValueError("Choose a production plan only after the complete composition review passes.")
        if self.allocated:
            require_gate(self.state)
        if not note.strip() or not executor.strip():
            raise ValueError("Record the composition choice and who made it.")
        if variant_id not in {v["id"] for v in self.state["plan"]["variants"]}:
            raise ValueError("Unknown variant ID.")
        keys = {f"{v['id']}/{c['id']}" for v in self.state["plan"]["variants"]
                if v["id"] == variant_id for c in v["cards"]}
        if self.quality_v2:
            graph = dependencies(self.state["plan"], self.state["canon"])
            if any(graph[k] - keys for k in keys):
                raise ValueError("Selected plan depends on an unproduced variant's canon source.")
        selection = {"variant_id": variant_id, "note": note, "executor": executor, "time": now()}
        self.state.update(production_variant_ids=[variant_id], phase="next_card", status="running", reason="")
        write(self.run / "production-selection.json", selection)
        event(self.run, "production_plan_selected", **selection)
        self.save()

    def find(self, key):
        return next((v, c) for v, c, k in self.cards() if k == key)

    def validate_plan(self, plan):
        validate(plan, self.plan_schema)
        if len(plan["variants"]) != self.brief["variants"]:
            raise ValueError("Variant count differs from the brief.")
        variant_ids, global_entities = set(), {e["id"]: e for e in self.state["canon"] if e["scope"] == "collection"}
        reference_ids = {r["id"] for r in self.refs}
        for variant in plan["variants"]:
            if 'production_contract' in self.state and not variant['wishes_response'].strip():
                raise ValueError('Each variant must explain how the producer wishes are addressed.')
            if variant["id"] in variant_ids:
                raise ValueError("Duplicate variant ID.")
            variant_ids.add(variant["id"])
            ids = [c["id"] for c in variant["cards"]]
            subjects = [c["subject"].strip().casefold() for c in variant["cards"]]
            if len(set(ids)) != 10 or len(set(subjects)) != 10:
                raise ValueError("Duplicate card IDs or identical subjects within a set.")
            categories = {c['background'] for c in variant['cards']}
            if (not {1, 2, 3, 4} <= categories if self.allocated else categories != {1, 2, 3, 4}):
                raise ValueError("Each variant must use all four background categories.")
            entity_ids = set(global_entities)
            declarations = set()
            for entity in variant["entities"]:
                if entity["id"] in declarations:
                    raise ValueError("Duplicate entity declaration.")
                declarations.add(entity["id"])
                entity_ids.add(entity["id"])
                if entity["scope"] == "set" and entity["id"] in global_entities:
                    raise ValueError("A local entity cannot shadow a collection entity ID.")
                if entity["scope"] == "collection":
                    old = global_entities.get(entity["id"])
                    if old and any(old[k] != entity[k] for k in ("description", "invariants", "allowed_changes")):
                        raise ValueError("Collection entity differs from its existing declaration/canon.")
                    global_entities[entity["id"]] = entity
            for card in variant["cards"]:
                if self.config["rules"]["exclude_main_cast_scenes"] and card["main_cast_scene"]:
                    raise ValueError("Key-cast scenes are excluded in this project.")
                if not set(card["reference_ids"]).issubset(reference_ids):
                    raise ValueError("Unknown reference ID.")
                if not set(card["entity_ids"]).issubset(entity_ids):
                    raise ValueError("Unknown entity ID.")
                if len(card["entity_ids"]) != len(set(card["entity_ids"])):
                    raise ValueError("Duplicate entity ID on card.")
        # Collection declarations are deliberately required before their first use.
        # This keeps preservation unambiguous when variants are rendered sequentially.
        if self.quality_v2:
            check_concepts(plan, self.state["canon"])
        if self.allocated:
            check_allocation(plan, self.state['allocation_policy'])
        if self.state.get('nameplate_policy'):
            from .nameplate import preflight
            from .ui_skin import measure_label
            checked=preflight(plan,self.state['nameplate_policy'],measure_label)
            write(self.run/'nameplate-check.json',checked)
            job=getattr(self,'_active_job_id',None)
            if job:write(self.run/'jobs'/job/'nameplate-check.json',checked)
            if checked['verdict']!='pass':
                failures=[f"{x['key']}: {x['text']} ({x['measured_width']} / {x['available_width']} px)" for x in checked['labels'] if x['verdict']!='pass']
                raise ValueError('English nameplate preflight failed before art: '+ '; '.join(failures)+'. Rewrite the title; if no fitting synonym preserves meaning, replace the card concept. No shrinking, ellipsis or widening.')

    def criteria(self, phase):
        names = list(CRITERIA[phase])
        if self.quality_v2:
            names += {"plan_review": ["visible_interest", "world_logic", "metadata_faithfulness"],
                      "card_review": ["geometry", "thumbnail_readability", "revision_fidelity"],
                      "set_review": ["interest", "thumbnail_readability"]}.get(phase, [])
        if self.allocated and phase == 'plan_review':
            names += ['rarity_assignment', 'category_assignment', 'cast_policy']
        if 'production_contract' in self.state and phase in ('plan_review','card_review','set_review'):
            names += ['producer_wishes']
        return names

    def art_history(self, key):
        invalid = set(self.state.get('art_invalidated', []))
        return [{**r, 'eligible': r['eligible'] and r['artifact']['job_id'] not in invalid}
                for r in self.state.get('art_history', {}).get(key, [])]

    def best_art(self, key):
        eligible = [r for r in self.art_history(key) if r['eligible']]
        return max(eligible, key=lambda r: r['quality']) if eligible else None

    def entity_specs(self, variant, card):
        specs = {}
        for entity in self.state["canon"]:
            if entity["scope"] == "collection" or entity.get("variant_id") == variant["id"]:
                specs[entity["id"]] = entity
        for group in self.state["plan"]["variants"]:
            for entity in group["entities"]:
                if entity["scope"] == "collection" or group["id"] == variant["id"]:
                    specs.setdefault(entity["id"], entity)
        return [specs[e] for e in card["entity_ids"]]

    def entity_review_targets(self):
        if not self.separate_entities:return []
        from .quality import identity
        pinned={identity(e,e.get('variant_id')) for e in self.state['canon']}
        targets=[];seen=set()
        for variant,card,key in self.cards():
            artifact=self.state['latest'].get(key)
            if not artifact or not any(h['artifact']['job_id']==artifact['job_id'] for h in self.state['art_history'].get(key,[])):continue
            for entity in self.entity_specs(variant,card):
                token=identity(entity,variant['id'])
                if token in pinned or token in seen or entity.get('source_card_id')!=key:continue
                seen.add(token)
                if any(h['artifact']['job_id']==artifact['job_id'] and h['entity_id']==entity['id']
                       for h in self.state.get('entity_history', [])):continue
                targets.append((key,entity['id']))
        return targets

    def qualify_entity(self, response, request):
        """Certify only the visible identity, never the rejected source card."""
        validate(response,ENTITY_REVIEW)
        context=request['context'];entity=context['entity'];source=context['source_artifact']
        key=entity['source_card_id'];variant,card=self.find(key)
        if (response['entity_id']!=entity['id'] or response['source_card_id']!=key
                or response['artifact_path']!=source['path']):
            raise ValueError('Entity review does not match the exact requested entity/source artifact.')
        if entity['id'] not in card['entity_ids'] or not any(e['id']==entity['id'] and e.get('source_card_id')==key for e in self.entity_specs(variant,card)):
            raise ValueError('Unrelated entity or undeclared canon source.')
        if sha(inside(self.run,source['path']))!=source['sha256']:
            raise ValueError('Entity source pixels changed.')
        known=[h['artifact'] for h in self.state['art_history'].get(key,[])]
        if not any(a['job_id']==source['job_id'] and a['sha256']==source['sha256'] and a['path']==source['path'] for a in known):
            raise ValueError('Entity review requires a recorded source candidate.')
        criteria=response['criteria'];required={'identity','anatomy_geometry','materials','style','visibility'}
        if len(criteria)!=5 or {c['name'] for c in criteria}!=required:
            raise ValueError('Entity review requires five distinct identity-specific criteria.')
        passed=response['verdict']=='pass'
        if passed and (not response['full_inspected'] or not response['thumbnail_inspected']
                       or any(c['verdict']!='pass' for c in criteria) or not response['approved_components']):
            raise ValueError('Entity pass requires actual inspection, approved components and all identity criteria passing.')
        record={'entity_id':entity['id'],'source_card_id':key,'artifact':copy.deepcopy(source),
                'review_job':getattr(self,'_active_job_id',None),**copy.deepcopy(response)}
        self.state.setdefault('entity_history',[]).append(record)
        if passed:
            from .quality import identity
            token=identity(entity,variant['id'])
            if not any(identity(e,e.get('variant_id'))==token for e in self.state['canon']):
                item=copy.deepcopy(entity)
                if item['scope']=='set':item['variant_id']=variant['id']
                old=self.state.get('orchestration',{}).get('invalidated_canon',[])
                version=1+max((e.get('version',1) for e in old if identity(e,e.get('variant_id'))==token),default=0)
                item.update(version=version,images=[source['path']],established_by=key,automatic=True,
                            qualification='entity_only',identity_source_artifact=copy.deepcopy(source),
                            identity_review_job=record['review_job'],approved_components=response['approved_components'],
                            excluded_components=response['excluded_components'])
                self.state['canon'].append(item)
                write(self.run/'canon.json',self.state['canon'])
                event(self.run,'entity_qualified',entity=entity['id'],source=key,source_job=source['job_id'],review_job=record['review_job'])
        else:
            self.state['feedback']['entity:'+key+':'+entity['id']]=copy.deepcopy(response)
        self.state['phase']='next_card'

    def references(self, variant=None, card=None):
        ids = list(self.config["global_reference_ids"])
        if card:
            ids += card["reference_ids"]
        images = [{"path": "inputs/" + r["path"], "role": f"original_style_reference:{r['id']}"}
                  for r in self.refs if r["id"] in ids]
        if card:
            for entity in self.entity_specs(variant, card):
                images += [{"path": p, "role": f"canon:{entity['id']}:v{entity['version']}"}
                           for p in entity.get("images", [])]
        return images

    def prepare(self):
        phase = self.state["phase"]
        context = {"brief": self.brief, "rules": self.config["rules"], "style": self.style}
        if self.state.get('nameplate_policy'):
            context['nameplate_policy']=self.state['nameplate_policy']
        if self.allocated:
            context['allocation_policy'] = self.state['allocation_policy']
        if 'production_contract' in self.state:
            context['production_contract'] = self.state['production_contract']
        if self.allocated:
            if phase in ('image', 'card_review', 'set_review'):
                require_gate(self.state)
                if sha(self.run / 'allocation.json') != self.state['allocation_gate']['file_sha256']:
                    raise ValueError('Approved pre-art allocation artifact changed.')
        images, schema = [], self.review_schema
        role, key = "reviewer", phase
        instruction = ""
        if phase == "brief_review":
            instruction = "Check whether the brief is understandable and internally consistent. Do not add requirements."
        elif phase == "plan":
            role, schema = "planner", self.plan_schema
            context.update(reference_catalog=self.refs, collection_canon=self.state["canon"])
            images = self.references()
            images += self.canon_images()
            instruction = ("Design 3–5 distinct interpretations of this ONE theme/genre, as requested by brief.variants. "
                           "Exactly 10 cards in each, all four background categories. Different semantic roles, "
                           "Respect the supplied cast policy. Write concrete art briefs. Reference IDs must exist in the catalog. "
                           "Declare recurring entities only when intentional identity continuity is useful. "
                           "Use English art instructions and English card/set titles, regardless of producer language; "
                           "write wishes_response in the producer's language. Prefer short one- or two-word titles "
                           "matching the actual object or clear scene hook, like Flower Arch or Bird Feeder. "
                           "Check English titles in report nameplates without truncation or unreadable shrinking. "
                           "Localizations and localization QA are out of scope. "
                           "Do not draw stars, rarity badges or UI. "
                           "Return the complete plan JSON, not commentary.")
            if self.allocated:
                instruction += (" Before any images, allocate each card's stars (1..5), golden flag, illustration "
                                "category (background), key_character_count, category_reason and rarity_reason. "
                                "Follow allocation_policy.star_counts, not an inferred reference formula. "
                                "All categories 1,2,3,4 must be present in EVERY set, independently of stars. "
                                "Category 5 is optional for golden scenes with one or two key characters. "
                                "Five stars do not automatically mean golden or more elaborate art. "
                                "Each variant needs cast_reason explaining the deliberate presence/absence of cast. "
                                "Do not carry a no-gold/no-characters restriction from another theme. "
                                "If characters are required, include them; optional allows a deliberate justified choice.")
                instruction += (" A ban on golden cards is NOT a ban on characters. Ordinary character scenes may "
                                "use category 4, golden=false and a positive key_character_count. ")
            if 'production_contract' in self.state:
                instruction += ("For EACH variant fill wishes_response: explain precisely how the producer's original "
                                "free-form wishes are reflected in its cards. Do not replace the user's wishes with "
                                "your own production assumptions. If wishes are empty say so. No silent one-variant reduction.")
            if self.state.get('nameplate_policy'):
                instruction += (' English titles must fit the supplied fixed nameplate policy BEFORE art. '
                                'Rewrite long names first; if no meaningful short synonym fits, replace the card concept. '
                                'Never shrink the font, clip, add ellipsis or widen the card to make a title pass. '
                                'The code measures pixel width with the delivered bitmap letterforms, not character count. ')
        elif phase == "plan_review":
            context.update(plan=self.state["plan"], collection_canon=self.state["canon"])
            images = self.references() + self.canon_images()
            instruction = "Evaluate the complete proposed sets against the original brief, rules and reference style."
        elif phase == 'entity_review':
            key=self.state['current'];variant,card=self.find(key)
            entity=next(e for e in self.entity_specs(variant,card) if e['id']==self.state['current_entity'])
            artifact=copy.deepcopy(self.state['latest'][key])
            context.update(entity=entity,source_artifact=artifact,card=card,variant={'id':variant['id'],'title':variant['title']})
            images=self.references(variant,card)
            thumb=self.run/'previews'/(key.replace('/','--')+'-'+artifact['job_id']+'-entity.png')
            thumb.parent.mkdir(exist_ok=True)
            with Image.open(self.run/artifact['path']) as source:
                preview=source.convert('RGB');preview.thumbnail((108,120),Image.Resampling.LANCZOS);preview.save(thumb)
            images[:0]=[{'path':artifact['path'],'role':'identity_source_candidate'},
                        {'path':thumb.relative_to(self.run).as_posix(),'role':'identity_source_thumbnail_108x120'}]
            schema=ENTITY_REVIEW;key=key+':'+entity['id']+':'+artifact['job_id']
            instruction=('Independently inspect ONLY the declared entity identity in the exact source PNG, full native and thumbnail. '
                         'The source card may fail for unrelated props/backgrounds. Do not accept the card or qualify excluded objects. '
                         'Return entity_id, source_card_id and artifact_path exactly. Five criteria: identity, anatomy_geometry, materials, style, visibility. '
                         'Materials/style apply to the entity itself. Explicitly list approved identity components and excluded props/background defects. '
                         'Pass only if identity is visibly coherent, reusable, adequately shown and all five criteria pass; uncertain if not visible. '
                         'The resulting full image will be identity-only reference; it is not an approved rendering/style reference for its excluded components.')
        elif phase in {"image", "card_review"}:
            key = self.state["current"]
            variant, card = self.find(key)
            context.update(variant={k: v for k, v in variant.items() if k != "cards"}, card=card,
                           canon=self.entity_specs(variant, card))
            if self.scored_art:
                context.update(art_acceptance=self.state['art_acceptance'], art_history=self.art_history(key))
            images = self.references(variant, card)
            if phase == "image":
                role, schema = "image", IMAGE
                latest = self.state["latest"].get(key)
                best = self.best_art(key) if self.scored_art else None
                if best:
                    latest = best['artifact']
                if latest:
                    images.insert(0, {"path": latest["path"], "role": "edit_target:best_eligible" if best else "edit_target:previous_attempt"})
                instruction = ("Create ONE original full-image object or character-scene illustration from the art brief. "
                               "No collage, card frame, stars, labels or game UI. Match ORIGINAL style references. "
                               "On revisions change only the reported problems and preserve approved parts. "
                               "Respect canon identity and the listed allowed changes. Export a PNG. "
                               "Return image_path relative to run root (or an absolute local path), plus note.")
                if self.separate_entities:
                    instruction += (' Canon may be qualified independently from a rejected full card. Reuse ONLY approved_components of canon identity; '
                                    'do not inherit excluded_components, old background/pose/props or their defects. Change strategy on plateau; '
                                    'the budget is three edits after the initial image, never a reset. ')
            else:
                artifact = self.state["latest"][key]
                if self.scored_art:
                    schema = scored_card_review(self.config.get('schema_version', 1), allocated=self.allocated)
                images.insert(0, {"path": artifact["path"], "role": "candidate_to_review"})
                if self.quality_v2 or self.scored_art:
                    thumb = self.run / "previews" / (key.replace("/", "--") + "-" + artifact["job_id"] + ".png")
                    thumb.parent.mkdir(exist_ok=True)
                    with Image.open(self.run / artifact["path"]) as source:
                        preview = source.convert("RGB")
                        preview.thumbnail((108, 120), Image.Resampling.LANCZOS)
                        preview.save(thumb)
                    images.insert(1, {"path": thumb.relative_to(self.run).as_posix(), "role": "candidate_thumbnail_108x120"})
                instruction = ("Inspect the candidate image against the brief and original reference images. "
                               "Use the actual visible evidence. Check silhouette at thumbnail scale, focal hierarchy, "
                               "materials, lighting, background category, UI absence and canon identity. "
                               "If no recurring entity exists, canon passes as not applicable. "
                               "A generated style match is a judgement, not a measured guarantee.")
                if self.scored_art:
                    instruction += (" Return art_review with all nine scores (0..100, step 5) and visible evidence. "
                                    "Q95 is computed by the CLI; do not supply an aggregate score. Acceptance requires "
                                    "unrounded Q95 >=93.5, all criteria pass, no blockers, class_pass and canon_pass. "
                                    "Inspect the full native file AND thumbnail; mark both inspection fields honestly. "
                                    "Class means explicit brief requirements for rarity/epic/gold, not inferred star meaning; "
                                    "when unspecified mark class_pass=true and explain not applicable in class_evidence. "
                                    "Below threshold or on a failed gate, give actionable feedback and next_strategy. "
                                    "After a stagnant edit (<0.5 gain against best eligible), change correction strategy. "
                                    "Use the supplied effective art_acceptance policy. Under version2, plateau changes strategy, "
                                    "not an early stop; after three failed edits discard the card unaccepted. Legacy version1 retains its snapshot. "
                                    "Never boost scores to force a pass.")
                if self.allocated:
                    instruction += (" Return observed_category and category_evidence from the actual image. "
                                    "It must match the approved card.background; never relabel a mismatch silently. "
                                    "Class is specified by card.stars/golden and cast policy, not N/A as missing data. "
                                    "Stars do not change object rendering; check gold/cast requirements explicitly.")
        elif phase == "set_review":
            context.update(plan=self.state["plan"], collection_canon=self.state["canon"])
            if self.state.get('discarded_sets'):
                ids=self.state.get('review_variant_ids',[])
                context.update(plan={'variants':[v for v in self.state['plan']['variants'] if v['id'] in ids]},
                               composition_options=self.state['plan'],discarded_sets=self.state['discarded_sets'])
            if self.production_ids():
                context["plan"] = {"variants": [v for v in self.state["plan"]["variants"]
                                                 if v["id"] in self.production_ids()]}
                context["composition_options"] = self.state["plan"]
            images = self.references() + self.canon_images()
            from .report import contact_sheets
            for relative in contact_sheets(self.run, self.state, self.config, production_only=True):
                if self.state.get('discarded_sets') and Path(relative).stem not in self.state.get('review_variant_ids',[]):continue
                images.append({"path": relative, "role": "candidate_set_contact_sheet"})
            # Full individual images are accessible by path to file/command agents;
            # sheets keep remote vision inputs small enough for normal models.
            context["accepted_images"] = self.state["accepted"]
            if self.state.get('discarded_sets'):
                context['accepted_images']={k:a for k,a in self.state['accepted'].items() if k.split('/')[0] in self.state.get('review_variant_ids',[])}
            instruction = ("Compare all complete sets and their contact sheets. Check thematic coherence, "
                           "semantic/visual variety, common art quality, genre interpretation and differences "
                           "between variants. Avoid imposing identical palettes/backgrounds. For local defects "
                           "return action=revise and exact variant_id/card_id keys in affected_cards. "
                           "For a defective composition return action=replan. Do not approve incomplete sets.")
        else:
            raise ValueError(f"Unknown phase: {phase}")
        task_key = f"{phase}:{key}"
        feedback = self.state["feedback"].get(task_key, "")
        if phase == "image":
            feedback = self.state["feedback"].get("image:" + key, feedback)
        if phase in CRITERIA:
            instruction += (" Return one criterion for EACH of these exact names: "
                            + ", ".join(self.criteria(phase))
                            + ". Every verdict needs concrete evidence. Any failed criterion means fail; "
                              "any uncertainty means uncertain. On pass use action=none and affected_cards=[]. "
                              "A conflict needing producer input uses action=clarify. "
                              "Do not invent scores or assert unavailable facts.")
            if 'production_contract' in self.state and phase in ('plan_review','card_review','set_review'):
                instruction += (" Assess the producer's original free-form wishes semantically, with concrete card "
                                "evidence. A wished-for feature cannot be omitted just because it lacks a separate "
                                "structured field. Mark producer_wishes fail on an unmet wish, or uncertain on a "
                                "real conflict. For full delivery review all requested sets, not one selected set.")
        if self.quality_v2:
            context["quality_guide"] = (files("setforge") / "defaults/quality-guide.md").read_text(encoding="utf-8")
            instruction += ("\nQUALITY CONTRACT v2: Use concrete subject, role, visible hook and silhouette; "
                            "metadata must faithfully describe the actual scene. Familiar props can be useful "
                            "anchors; novelty and background distribution have no numeric quotas. Compare meanings, "
                            "not just different labels. Reject ten similar props or variants differing only in palette. "
                            "Every new recurring entity names its best full-view source_card_id (variant/card), "
                            "which is generated before secondary appearances. Do not freeze a tiny background. "
                            "For reviews return structured findings, exact targets, visible evidence and a targeted fix. "
                            "Every failed criterion needs a blocking finding; no blocking finding may pass. "
                            "For art inspect the full file AND its thumbnail; count visible parts with locations, "
                            "allow natural occlusion, inspect attachments and materials separately. A pretty image "
                            "cannot compensate for wrong geometry. Check new defects after edits. "
                            "If counting or identity is not visible, use uncertain rather than guessing. "
                            "Do not confuse celestial stars or golden objects with game UI/rarity.")
        request = {"protocol_version": 1, "kind": phase, "task_key": task_key,
                   "instruction": instruction, "context": context, "feedback": feedback,
                   "images": images, "output_schema": schema}
        if phase in CRITERIA:
            request["required_criteria"] = self.criteria(phase)
        if phase == "image" and self.config["providers"]["image"].get("text_only_draft", False):
            if any(i["role"].startswith(("canon:", "edit_target:")) for i in images):
                raise ValueError("Text-only draft worker cannot preserve image canon or perform edits")
            request["conditioning"] = {"mode": "text_only_draft", "reference_images_supplied": 0,
                                       "omitted_original_ids": [i["role"] for i in images]}
            request["images"] = images = []
            instruction = instruction.replace("Match ORIGINAL style references.",
                                              "No reference images are supplied; approximate the textual style guide.")
            request["instruction"] = instruction
        if phase == "image":
            render_instruction = instruction.split("Return image_path", 1)[0]
            request["prompt"] = (render_instruction + "\n\nSTYLE\n" + self.style + "\n\nBRIEF\n"
                                 + json.dumps(context, ensure_ascii=False) + "\n\nINPUT IMAGES IN ORDER\n"
                                 + json.dumps(images, ensure_ascii=False) + "\n\nTARGETED FEEDBACK\n" + str(feedback))
        return request, role

    def canon_images(self):
        images, seen = [], set()
        for entity in self.state["canon"]:
            for path in entity.get("images", []):
                if path not in seen:
                    images.append({"path": path, "role": f"canon:{entity['id']}:v{entity['version']}"})
                    seen.add(path)
        return images

    def issue(self):
        request, role = self.prepare()
        limits, state = self.config["limits"], self.state
        key = request["task_key"]
        attempt_key, attempt_limit = key, limits['max_attempts_per_task']
        if self.scored_art and request['kind'] == 'image':
            attempt_limit = state['art_acceptance']['max_edits'] + 1
            if state['art_stops'].get(state['current']) in ('plateau', 'iteration_limit', 'discarded'):
                self.block('Image stopped without acceptance: ' + state['art_stops'][state['current']])
        elif self.scored_art and request['kind'] == 'card_review':
            attempt_key += ':' + state['latest'][state['current']]['job_id']
        if state["task_attempts"].get(attempt_key, 0) >= attempt_limit:
            if request['kind']=='image' and self.separate_entities:
                key=state['current'];self.discard_card(key,'Initial generation and three edit attempts exhausted, including recorded generator failures.')
                state['art_stops'][key]='discarded';state.update(phase='next_card',pending=None)
                self.save();return
            self.block(f"Attempt limit reached for {key}. Inspect the history before continuing.")
        cost = self.config["providers"][role]["estimated_cost"]
        if state["calls"] >= limits["max_calls"]:
            self.block("Total call limit reached.")
        if state["estimated_cost"] + cost > limits["max_estimated_cost"] + 1e-9:
            self.block("Estimated cost limit reached.")
        number = len(state["jobs"]) + 1
        job_id = f"{number:04d}-{request['kind']}"
        job = self.run / "jobs" / job_id
        # An orphan prepared directory can remain after a crash before state commit;
        # no provider is dispatched until the pending state has been persisted.
        job.mkdir(parents=True, exist_ok=True)
        request["job_id"] = job_id
        request["suggested_output_path"] = (job.relative_to(self.run) / "generated.png").as_posix()
        write(job / "request.json", request)
        write(job / "schema.json", request["output_schema"])
        state["pending"] = {"id": job_id, "role": role, "task_key": key, "request_sha256": sha(job / "request.json")}
        state["calls"] += 1
        state["estimated_cost"] = round(state["estimated_cost"] + cost, 8)
        state["task_attempts"][attempt_key] = state["task_attempts"].get(attempt_key, 0) + 1
        state["jobs"].append({"id": job_id, "kind": request["kind"], "status": "pending", "time": now(),
                              "provider": copy.deepcopy(self.config["providers"][role]), "task_key": key})
        state.update(status="running", reason="")
        event(self.run, "job_issued", id=job_id, kind=request["kind"], role=role, estimated_cost=cost)
        self.save()

    def accept(self, response, executor="external", model="unspecified"):
        pending = self.state["pending"]
        if not pending:
            raise ValueError("No pending job.")
        job = self.run / "jobs" / pending["id"]
        if sha(job / "request.json") != pending["request_sha256"]:
            raise ValueError("Pending request was modified.")
        request = read(job / "request.json")
        validate(response, request["output_schema"])
        phase = request["kind"]
        if self.allocated and phase in ('image', 'card_review', 'set_review'):
            require_gate(self.state)
        if phase in CRITERIA:
            self.validate_review(response, phase)
        if phase == "image":
            response = self.import_image(response, job)
        write(job / "response.json", response)
        write(job / "provenance.json", {"executor": executor, "model": model, "received": now(),
                                        "request_sha256": pending["request_sha256"]})
        record = next(j for j in self.state['jobs'] if j['id'] == pending['id'])
        self._active_job_id = pending['id']
        record.update(status="received", executor=executor, model=model)
        self.state["pending"] = None
        event(self.run, "response_received", id=pending["id"], verdict=response.get("verdict"))
        if phase == "plan":
            try:
                self.validate_plan(response)
            except ValueError as exc:
                record["status"] = "rejected"
                record["reason"] = str(exc)
                self.state["feedback"][pending["task_key"]] = str(exc)
                event(self.run, "gate_failed", gate="composition_code", reason=str(exc))
                self.save()
                return
            self.state.pop("production_variant_ids", None)
            self.state.update(plan=response, phase="plan_review")
            if self.allocated:
                self.state['allocation_gate'] = None
            write(self.run / "plans.json", response)
        elif phase == "image":
            key = self.state["current"]
            path = response["image_path"]
            with Image.open(self.run / path) as image:
                artifact = {"path": path, "sha256": sha(self.run / path), "size": list(image.size),
                            "job_id": pending["id"]}
            self.state["latest"][key] = artifact
            self.state["phase"] = "card_review"
        elif phase == 'entity_review':
            self.qualify_entity(response, request)
        else:
            self.apply_review(response, phase)
        self.save()

    def validate_review(self, response, phase):
        items = response["criteria"]
        if len({c["name"] for c in items}) != len(items):
            raise ValueError("Duplicate review criterion.")
        if not set(self.criteria(phase)).issubset(c["name"] for c in items):
            raise ValueError("Review does not cover all required criteria.")
        if self.quality_v2 and {c["name"] for c in items} != set(self.criteria(phase)):
            raise ValueError("Review adds unknown criteria; do not invent requirements")
        verdicts = {c["verdict"] for c in items}
        expected = "fail" if "fail" in verdicts else "uncertain" if "uncertain" in verdicts else "pass"
        if response["verdict"] != expected:
            raise ValueError("Overall verdict contradicts criterion verdicts.")
        if expected == "pass" and (response["action"] != "none" or response["affected_cards"]):
            raise ValueError("Passed review must have action=none and no affected cards.")
        if expected != "pass" and response["action"] == "none":
            raise ValueError("Failed/uncertain review needs an action.")
        if phase == "set_review" and expected == "fail" and response["action"] == "revise":
            keys = {k for _, _, k in self.cards()}
            if not response["affected_cards"] or not set(response["affected_cards"]).issubset(keys):
                raise ValueError("Set revision must name existing variant/card keys.")
        if self.quality_v2:
            check_findings(response, phase, {k for _, _, k in self.cards()})
        if phase == 'card_review' and self.scored_art:
            art = response['art_review']
            q = art_score(art['scores'])
            if (any(not value.strip() for value in art['evidence'].values())
                    or not art['class_evidence'].strip()):
                raise ValueError('Visible evidence is required for every score and class gate.')
            if not art['full_inspected'] or not art['thumbnail_inspected']:
                raise ValueError('Full native and thumbnail inspection are required; no uninspected scores.')
            category_matches = True
            if self.allocated:
                category_matches = art['observed_category'] == self.find(self.state['current'])[1]['background']
                if not art['category_evidence'].strip():
                    raise ValueError('Observed category needs visible evidence.')
            if (q < self.state['art_acceptance']['threshold'] or expected != 'pass'
                    or not art['class_pass'] or not art['canon_pass'] or not category_matches):
                if not art['feedback'].strip() or not art['next_strategy'].strip():
                    raise ValueError('A non-accepted image requires actionable feedback and next_strategy.')

    def import_image(self, response, job):
        source = Path(response["image_path"])
        if not source.is_absolute():
            source = inside(self.run, source)
        if not source.is_file():
            raise ValueError("Returned image file does not exist.")
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            minimum = self.config["rules"]["output_size"]
            if image.width < minimum[0] or image.height < minimum[1]:
                raise ValueError("Image is smaller than the configured export size.")
            if image.format != "PNG":
                raise ValueError("Image worker must return a PNG; raw outputs are preserved unchanged.")
            if self.quality_v2:
                expected_ratio = minimum[0] / minimum[1]
                tolerance = self.config["rules"].get("aspect_tolerance", 0.03)
                if abs((image.width / image.height) / expected_ratio - 1) > tolerance:
                    raise ValueError("Image aspect differs from the target; request a matching canvas, do not crop subject")
        digest = sha(source)
        existing = [a["sha256"] for k, a in self.state["accepted"].items() if k != self.state["current"]]
        original_hashes = {r["sha256"] for r in self.refs}
        if digest in existing or digest in original_hashes:
            raise ValueError("Image duplicates another accepted card or an original reference.")
        if self.quality_v2:
            pixels = pixel_digest(source)
            forbidden = [self.run / a["path"] for a in self.state["accepted"].values()]
            forbidden += [self.run / "inputs" / r["path"] for r in self.refs]
            previous = self.state["latest"].get(self.state["current"])
            if previous:
                forbidden.append(self.run / previous["path"])
            if any(p.exists() and pixel_digest(p) == pixels for p in forbidden):
                raise ValueError("Duplicate pixels: unchanged rejected image, accepted card or original reference")
        dest = job / "artifact.png"
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        return {"image_path": dest.relative_to(self.run).as_posix(), "note": response["note"]}

    def apply_review(self, review, phase):
        event(self.run, "gate_result", gate=phase, verdict=review["verdict"], summary=review["summary"])
        if review["verdict"] == "uncertain" or review["action"] == "clarify":
            self.block(review["summary"])
        if phase == "brief_review":
            if review["verdict"] != "pass":
                self.block("Brief conflict: " + review["summary"])
            self.state["phase"] = "plan"
        elif phase == "plan_review":
            if review["verdict"] == "pass":
                self.state.pop("production_variant_ids", None)
                self.state["phase"] = 'allocation_check' if self.allocated else ('plan_selection' if self.needs_plan_selection() else "next_card")
            else:
                self.state["feedback"]["plan:plan"] = review
                self.state["phase"] = "plan"
        elif phase == "card_review":
            key = self.state["current"]
            if self.scored_art:
                return self.apply_scored_art(review, key)
            if review["verdict"] == "pass":
                self.state["accepted"][key] = copy.deepcopy(self.state["latest"][key])
                self.freeze_canon(key)
                self.state["phase"] = "next_card"
            elif review["action"] == "replan":
                self.replan(review)
            else:
                self.state["feedback"]["image:" + key] = review
                self.state["phase"] = "image"
        elif phase == "set_review":
            if review["verdict"] == "pass":
                if self.state.get('discarded_sets'):
                    self.state['reviewed_variants']=list(self.state.get('review_variant_ids',[]))
                    self.state.update(status='completed_with_rejections',phase='discarded',reason='Viable complete sets reviewed; discarded sets are not delivered as complete.')
                    return
                self.state["phase"] = "export"
                return
            self.state["set_revisions"] += 1
            if self.state["set_revisions"] > self.config["limits"]["max_set_revisions"]:
                self.block("Whole-set revision limit reached: " + review["summary"])
            if review["action"] == "replan":
                self.replan(review)
            else:
                for key in review["affected_cards"]:
                    if self.scored_art and key in self.state['accepted']:
                        self.state['art_invalidated'].append(self.state['accepted'][key]['job_id'])
                        self.state['art_stops'].pop(key, None)
                    self.state["accepted"].pop(key, None)
                    self.state["feedback"]["image:" + key] = review
                self.state["phase"] = "next_card"

    def apply_scored_art(self, review, key):
        art = review['art_review']
        history = self.state['art_history'].setdefault(key, [])
        eligible = (review['verdict'] == 'pass' and art['class_pass'] and art['canon_pass']
                    and not any(f['severity'] == 'blocking' for f in review.get('findings', [])))
        card = self.find(key)[1]
        if self.allocated:
            eligible = eligible and art['observed_category'] == card['background']
        entry = {'iteration': len(history), 'quality': art_score(art['scores']),
                 'eligible': eligible, 'artifact': copy.deepcopy(self.state['latest'][key]),
                 'review_job': getattr(self, '_active_job_id', self.state['jobs'][-1]['id']), **copy.deepcopy(art)}
        history.append(entry)
        decision = art_decide(self.art_history(key), acceptance_policy=self.state['art_acceptance'])
        signal=plateau_signal(self.art_history(key),acceptance_policy=self.state['art_acceptance'])
        entry['plateau_signal']=signal
        self.state['art_stops'][key] = decision
        event(self.run, 'art_acceptance', card=key, quality=entry['quality'],
              iteration=entry['iteration'], decision=decision, eligible=eligible)
        if decision == 'accepted':
            self.state['accepted'][key] = {**copy.deepcopy(entry['artifact']),
                                          'quality': entry['quality'], 'scores': entry['scores'],
                                          'eligible': True, 'art_iteration': entry['iteration']}
            if self.allocated:
                self.state['accepted'][key].update(stars=card['stars'], golden=card['golden'],
                                                   observed_category=art['observed_category'])
            self.freeze_canon(key)
            self.state['phase'] = 'next_card'
        elif decision == 'discarded':
            self.discard_card(key,'Three edits exhausted: score below threshold or mandatory gate failed.')
            self.state['phase']='next_card'
        elif decision in ('plateau', 'iteration_limit'):
            self.state['accepted'].pop(key, None)
            self.state['phase'] = 'art_stopped'
            self.block(f'{key}: {decision}; below threshold or failed gates. NOT accepted.')
        elif review['action'] == 'replan':
            self.replan(review)
        else:
            self.state['feedback']['image:' + key] = review
            if signal and self.separate_entities:
                self.state['feedback']['image:'+key]['strategy_change_required']=True
                event(self.run,'plateau_strategy_change',card=key,iteration=entry['iteration'])
            self.state['phase'] = 'next_card' if self.separate_entities else 'image'

    def replan(self, review):
        self.state['feedback']['plan:plan'] = review
        if self.scored_art:
            invalid = set(self.state['art_invalidated'])
            for key, history in self.state['art_history'].items():
                invalid.update(r['artifact']['job_id'] for r in history)
                self.state['art_stops'][key] = 'replan_invalidated'
            self.state['art_invalidated'] = sorted(invalid)
        self.state.update(accepted={}, latest={}, plan=None, phase='plan')
        if self.allocated:
            self.state['allocation_gate'] = None
        self.state['canon'] = read(self.run / 'inputs/canon.json')
        # Keep cumulative generation counters: replanning cannot buy extra edits.

    def freeze_canon(self, key):
        if self.separate_entities and self.quality_v2:return  # New source-aware contracts qualify identity independently.
        variant, card = self.find(key)
        for entity in self.entity_specs(variant, card):
            if self.quality_v2 and entity.get("source_card_id") != key:
                continue
            scope_key = (entity["scope"], entity["id"], variant["id"] if entity["scope"] == "set" else None)
            if any((e["scope"], e["id"], e.get("variant_id")) == scope_key for e in self.state["canon"]):
                continue
            item = copy.deepcopy(entity)
            if item["scope"] == "set":
                item["variant_id"] = variant["id"]
            item.update(version=1, images=[self.state["accepted"][key]["path"]],
                        established_by=key, automatic=True)
            self.state["canon"].append(item)
            event(self.run, "canon_frozen", entity=item["id"], scope=item["scope"], source=key)
        write(self.run / "canon.json", self.state["canon"])

    def provider_failure(self, error):
        pending = self.state["pending"]
        next(j for j in self.state['jobs'] if j['id'] == pending['id']).update(status="error", reason=str(error))
        self.state["feedback"][pending["task_key"]] = str(error)
        event(self.run, "provider_error", id=pending["id"], reason=str(error))
        self.state["pending"] = None
        self.save()

    def advance(self):
        self.check_snapshot()
        if self.state["status"] == "needs_attention":
            raise NeedsInput(self.state["reason"])
        while self.state["status"] not in ('complete','completed_with_rejections'):
            phase = self.state["phase"]
            if phase == 'allocation_check':
                self.validate_plan(self.state['plan'])
                gate = check_allocation(self.state['plan'], self.state['allocation_policy'])
                write(self.run / 'allocation.json', gate)
                self.state['allocation_gate'] = {k: gate[k] for k in ('verdict', 'plan_sha256')}
                self.state['allocation_gate']['file_sha256'] = sha(self.run / 'allocation.json')
                event(self.run, 'gate_result', gate='pre_art_allocation', verdict='pass',
                      plan_sha256=gate['plan_sha256'])
                self.state['phase'] = 'plan_selection' if self.needs_plan_selection() else 'next_card'
                self.save()
                continue
            if phase == "plan_selection":
                self.state.update(status="waiting_for_selection", reason="Choose one reviewed composition using choose-plan before generating art.")
                self.save()
                raise NeedsInput(self.state["reason"])
            if phase == "next_card":
                if self.separate_entities:
                    targets=self.entity_review_targets()
                    if targets:
                        key,eid=targets[0];self.state.update(current=key,current_entity=eid,phase='entity_review');self.save();continue
                remaining = [key for _, _, key in self.cards() if key not in self.state["accepted"] and key not in self.state.get('discarded_cards',{})]
                if remaining:
                    if self.quality_v2:
                        deps = dependencies(self.state["plan"], self.state["canon"])
                        remaining = [k for k in remaining if not deps[k]] if self.separate_entities else [k for k in remaining if deps[k] <= set(self.state['accepted'])]
                        if not remaining:
                            if self.separate_entities:
                                for _,_,k in self.cards():
                                    if k not in self.state['accepted'] and k not in self.state.get('discarded_cards',{}):
                                        self.discard_card(k,'Required identity could not be qualified; no eligible source remains.')
                                        self.state['art_stops'][k]='discarded'
                                self.state['phase']='next_card';self.save();continue
                            self.block("No eligible card: unresolved canon dependency")
                    self.state.update(current=remaining[0], phase="image")
                else:
                    if self.state.get('discarded_sets'):
                        complete=[v['id'] for v in self.state['plan']['variants'] if all(v['id']+'/'+c['id'] in self.state['accepted'] for c in v['cards'])]
                        self.state['review_variant_ids']=complete
                        if not complete:
                            self.state.update(status='completed_with_rejections',phase='discarded',reason='No complete ten-card set passed.')
                            self.save();return
                    self.state["phase"] = "set_review"
                self.save()
                continue
            if phase == "export":
                from .report import export
                self.state["export_attempts"] = self.state.get("export_attempts", 0) + 1
                self.save()
                event(self.run,'export_started',attempt=self.state['export_attempts'])
                try:
                    export(self.run, self.state, self.config)
                except (OSError, ValueError) as exc:
                    event(self.run, "export_error", reason=str(exc), attempt=self.state["export_attempts"])
                    if self.state["export_attempts"] >= self.config["limits"]["max_attempts_per_task"]:
                        self.block("Export rebuild limit reached: " + str(exc))
                    continue
                event(self.run,'export_finished',attempt=self.state['export_attempts'])
                self.state.update(status="complete", phase="complete", reason="")
                event(self.run, "complete", cards=len(self.state["accepted"]))
                self.save()
                from .report import report
                report(self.run, self.state)
                return
            if not self.state["pending"]:
                self.issue()
                if not self.state['pending']:continue
            pending = self.state["pending"]
            config = self.config["providers"][pending["role"]]
            job = self.run / "jobs" / pending["id"]
            request = read(job / "request.json")
            if (job / "response.json").exists():
                provenance = read(job / "provenance.json") if (job / "provenance.json").exists() else {}
                self.accept(read(job / "response.json"), provenance.get("executor", "recovered"),
                            provenance.get("model", "unspecified"))
                continue
            if config["type"] == "files":
                self.state.update(status="waiting_for_worker", reason=f"Respond to {pending['id']}")
                self.save()
                raise NeedsInput(f"Worker request: {job / 'request.json'}")
            try:
                if (job / "worker-response.json").exists():
                    response = read(job / "worker-response.json")
                elif config["type"] == "image-cli" and (job / "generated.png").exists():
                    response = {"image_path": str(job / "generated.png"), "note": "Recovered completed CLI output"}
                else:
                    response = invoke(config, request, self.run, job, self.config["limits"]["timeout_seconds"])
                self.accept(response, executor=config["type"], model=config.get("model", "command-defined"))
            except NeedsInput:
                raise
            except (ValueError, OSError, KeyError, StopIteration) as exc:
                if not self.state["pending"]:
                    raise
                self.provider_failure(exc)

    def resume_attention(self, note, extensions=None):
        if self.scored_art and self.state['phase'] == 'art_stopped':
            raise ValueError('Art stopped without acceptance. continue cannot override plateau/five-edit policy; '
                             'review the saved candidates and use a new run for a changed brief or composition.')
        if not note.strip():
            raise ValueError("Provide a concrete resolution note.")
        if self.state["status"] != "needs_attention":
            raise ValueError("Run is not waiting for a decision.")
        event(self.run, "operator_resolution", note=note)
        for key, value in (extensions or {}).items():
            if value < 0:
                raise ValueError("Limit extension must be non-negative.")
            self.state.setdefault("limit_extensions", {})[key] = self.state.get("limit_extensions", {}).get(key, 0) + value
            self.config["limits"][key] += value
        if extensions:
            event(self.run, "limits_extended", extensions=extensions)
        self.state.update(status="running", reason="")
        # Re-run a gate instead of treating a human note as a passing verdict.
        phase = self.state["phase"]
        if phase in CRITERIA:
            key = f"{phase}:{self.state['current'] if phase == 'card_review' else phase}"
            self.state["feedback"][key] = note
        self.save()
