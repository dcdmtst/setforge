"""CLI contract: JSON on stdout, diagnostics on stderr, stable exit codes."""

import argparse
import hashlib
import json
import os
import math
import shutil
import sqlite3
import sys
import time
import zipfile
import tempfile
from importlib.util import find_spec
from pathlib import Path

from .engine import NeedsInput, Pipeline
from .io import event, inside, read, sha, write, locked as lock_run
from .project import check_project, init_project
from .report import contact_sheets, report
from .schemas import SCHEMAS, PLAN_V2, REVIEW_V2
from .acceptance import policy as art_policy
from .schemas import scored_card_review, production_plan
from .allocation import require_gate, check as check_allocation
from . import __version__


def output(value):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Must be a positive integer.')
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Must be a finite positive duration.')
    return number


def parser():
    from .construction import schema as construction_schema, extend_schema
    SCHEMAS['construction-review'] = construction_schema()
    SCHEMAS['construction-card-review'] = extend_schema(scored_card_review(2, allocated=True), 'card_review')
    p = argparse.ArgumentParser(prog="setforge", description="Collection-card production pipeline")
    p.add_argument("--version", action="version", version=f"setforge {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a project and import reference materials")
    init.add_argument("--project", type=Path, required=True)
    init.add_argument("--style-pack", "--materials", dest="materials", type=Path)
    style = sub.add_parser('import-style', help='Import local style references for future runs')
    style.add_argument('--project', type=Path, required=True)
    style.add_argument('--style-pack', type=Path, required=True)
    lettering=sub.add_parser('font-configure',help='Use a licensed local OTF/TTF for labels and pixel-fit QA; font files are never copied')
    font_target=lettering.add_mutually_exclusive_group(required=True)
    font_target.add_argument('--project',type=Path)
    font_target.add_argument('--run',type=Path)
    font_choice=lettering.add_mutually_exclusive_group()
    font_choice.add_argument('--font',type=Path)
    font_choice.add_argument('--neutral',action='store_true')
    for name in ('generators', 'probe-generators'):
        generator = sub.add_parser(name, help='Inspect capabilities or prepare an optional agent-executed probe')
        generator.add_argument('--project', type=Path, required=True)
        generator.add_argument('--capabilities', type=Path)
        if name == 'probe-generators':generator.add_argument('--out', type=Path, required=True)
    inbox = sub.add_parser('inbox', help='List local orders needing an active external agent')
    inbox.add_argument('--runs', type=Path, required=True)
    inbox.add_argument('--wait-seconds', type=positive_seconds, default=None)
    for name in ('export', 'result-manifest'):
        delivery = sub.add_parser(name, help='Verify complete delivery and produce final files')
        delivery.add_argument('--run', type=Path, required=True)
        if name == 'export':delivery.add_argument('--out', type=Path)
    doctor = sub.add_parser("doctor", help="Check project, tools and credential presence")
    doctor.add_argument("--project", type=Path, required=True)
    start = sub.add_parser("run", help="Create a run and advance until complete or waiting")
    start.add_argument("--project", type=Path, required=True)
    inputs = start.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--brief', type=Path)
    inputs.add_argument('--theme')
    start.add_argument('--wishes', help='Producer free-form wishes, preserved verbatim')
    start.add_argument('--variants', type=int, choices=[3,4,5])
    start.add_argument('--mode', choices=['full','pilot'], help='Full delivery by default; pilot must be explicit')
    start.add_argument('--artists', type=positive_int, default=2, help='Parallel image-worker capacity; default two, no fixed upper ceiling')
    start.add_argument('--reviewers',type=positive_int,default=2)
    start.add_argument('--workers',type=positive_int,help='Total physical executor capacity; default artists+1')
    start.add_argument("--run", type=Path, required=True)
    brief = sub.add_parser('brief', help='Save producer inputs without starting generation')
    brief.add_argument('--theme', required=True)
    brief.add_argument('--wishes', default='')
    brief.add_argument('--variants', type=int, choices=[3,4,5], default=3)
    brief.add_argument('--out', type=Path, required=True)
    ui = sub.add_parser('ui', help='Open a local producer form and results dashboard')
    ui.add_argument('--project', type=Path, required=True)
    ui.add_argument('--runs', type=Path)
    ui.add_argument('--port', type=int, default=8840)
    ui.add_argument('--artists', type=positive_int, default=2)
    ui.add_argument('--reviewers',type=positive_int,default=2)
    ui.add_argument('--workers',type=positive_int)
    for name in ("resume", "status", "report", "unlock"):
        item = sub.add_parser(name)
        item.add_argument("--run", type=Path, required=True)
    submit = sub.add_parser("submit", help="Submit a result for the exact pending job")
    submit.add_argument("--run", type=Path, required=True)
    submit.add_argument("--job", required=True)
    submit.add_argument("--response", type=Path, required=True)
    submit.add_argument("--executor", required=True)
    submit.add_argument("--model", default="unspecified")
    fail = sub.add_parser("fail", help="Record one failed external worker attempt and continue")
    fail.add_argument("--run", type=Path, required=True)
    fail.add_argument("--job", required=True)
    fail.add_argument("--reason", required=True)
    coordinate = sub.add_parser('coordinate', help='Process the parallel queue and advance gates without invoking providers')
    coordinate.add_argument('--run', type=Path, required=True)
    coordinate.add_argument('--artists', type=positive_int)
    coordinate.add_argument('--reviewers',type=positive_int)
    coordinate.add_argument('--workers',type=positive_int)
    coordinate.add_argument('--watch', action='store_true')
    coordinate.add_argument('--poll-seconds', type=positive_seconds, default=1)
    queue = sub.add_parser('queue', help='Inspect parallel jobs, leases and incoming results')
    queue.add_argument('--run', type=Path, required=True)
    claim = sub.add_parser('claim', help='Lease one job for an external worker')
    claim.add_argument('--run', type=Path, required=True)
    claim.add_argument('--worker', required=True)
    claim.add_argument('--role', choices=['image','reviewer','planner','flex'], required=True)
    claim.add_argument('--lease-seconds', type=positive_seconds, default=900)
    claim.add_argument('--wait-seconds',type=positive_seconds,default=None,help='Wait briefly for an eligible lease; no repeated process launch')
    for name in ('heartbeat', 'deliver', 'abandon'):
        command = sub.add_parser(name, help='Update an owned queue lease or deliver a result')
        command.add_argument('--run', type=Path, required=True)
        command.add_argument('--job', required=True)
        command.add_argument('--worker', required=True)
        command.add_argument('--token', required=True)
        if name == 'heartbeat':
            command.add_argument('--lease-seconds', type=positive_seconds, default=900)
        if name == 'deliver':
            command.add_argument('--response', type=Path, required=True)
            command.add_argument('--model', default='unspecified')
        if name == 'abandon':
            command.add_argument('--reason', required=True)
    reconcile = sub.add_parser('reconcile', help='Explicitly confirm an uncertain generation is no longer running and has no result')
    reconcile.add_argument('--run', type=Path, required=True)
    reconcile.add_argument('--job', required=True)
    reconcile.add_argument('--reason', required=True)
    worker = sub.add_parser('worker', help='Run a configured provider outside the coordinator lock')
    worker.add_argument('--run', type=Path, required=True)
    worker.add_argument('--worker', required=True)
    worker.add_argument('--role', choices=['image','reviewer','planner','flex'], required=True)
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--poll-seconds', type=positive_seconds, default=1)
    worker.add_argument('--lease-seconds', type=positive_seconds, default=900)
    prep=sub.add_parser('prepare-job',help='Read-only execution envelope with cached-reference status and framing guidance')
    prep.add_argument('--run',type=Path,required=True);prep.add_argument('--job',required=True);prep.add_argument('--worker',required=True)
    image_start = sub.add_parser('image-start', help='Prepare bounded tool inputs and open a machine-timed image attempt')
    image_start.add_argument('--run', type=Path, required=True)
    image_start.add_argument('--job', required=True)
    image_start.add_argument('--worker', required=True)
    image_start.add_argument('--token', required=True)
    image_start.add_argument('--tool', default='imagegen')
    image_start.add_argument('--model', default='unspecified')
    image_start.add_argument('--max-images', type=positive_int)
    image_start.add_argument('--prompt-file', type=Path, help='Exact augmented prompt, saved before the call')
    image_finish = sub.add_parser('image-finish', help='Close timer, preserve PNG, compute hash and write response JSON')
    image_finish.add_argument('--run', type=Path, required=True)
    image_finish.add_argument('--job', required=True)
    image_finish.add_argument('--worker', required=True)
    image_finish.add_argument('--token', required=True)
    result = image_finish.add_mutually_exclusive_group(required=True)
    result.add_argument('--image', type=Path)
    result.add_argument('--error')
    image_finish.add_argument('--note', default='Generated PNG; metadata recorded by Setforge.')
    cache=sub.add_parser('reference-cache',help='List uninspected references or explicitly record a completed visual inspection')
    cache.add_argument('--run',type=Path,required=True);cache.add_argument('--job',required=True);cache.add_argument('--worker',required=True)
    cache.add_argument('--mark-path');cache.add_argument('--role');cache.add_argument('--sha256');cache.add_argument('--evidence')
    check=sub.add_parser('validate-response',help='Read-only schema/target preflight; never art acceptance')
    check.add_argument('--run',type=Path,required=True);check.add_argument('--job',required=True);check.add_argument('--response',type=Path,required=True)
    cont = sub.add_parser("continue", help="Resolve attention state and re-run its gate")
    cont.add_argument("--run", type=Path, required=True)
    cont.add_argument("--note", required=True)
    cont.add_argument("--extra-calls", type=int, default=0)
    cont.add_argument("--extra-attempts", type=int, default=0)
    cont.add_argument("--extra-set-revisions", type=int, default=0)
    cont.add_argument("--extra-estimated-cost", type=float, default=0)
    pack = sub.add_parser("pack", help="Make a portable archive of the full run")
    pack.add_argument("--run", type=Path, required=True)
    pack.add_argument("--out", type=Path, required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("name", choices=SCHEMAS)
    schema.add_argument("--contract-version", type=int, choices=[1, 2], default=2)
    acceptance = sub.add_parser('acceptance-policy', help='Show the enforced art threshold, edit cap and plateau rule')
    acceptance.add_argument('--run', type=Path, help='Inspect the policy saved for an existing run')
    amendment=sub.add_parser('amend-policy',help='Explicitly adopt the current art policy without resetting history or attempts')
    amendment.add_argument('--run',type=Path,required=True)
    amendment.add_argument('--note',required=True)
    revision=sub.add_parser('revise-card',help='Reopen only an explicitly producer-rejected card, preserving completed history')
    revision.add_argument('--run',type=Path,required=True)
    revision.add_argument('--card',required=True)
    revision.add_argument('--note',required=True)
    titles=sub.add_parser('nameplate-check',help='Check fixed-size English titles before art without renaming them')
    title_source=titles.add_mutually_exclusive_group(required=True)
    title_source.add_argument('--run',type=Path)
    title_source.add_argument('--plan',type=Path)
    titles.add_argument('--out',type=Path)
    titles.add_argument('--project',type=Path,help='Font profile for --plan; --run uses its own saved typography')
    allocation = sub.add_parser('allocation-check', help='Check the approved pre-art category, stars and cast allocation')
    allocation.add_argument('--run', type=Path, required=True)
    select = sub.add_parser("select", help="Record the producer's final variant choice")
    select.add_argument("--run", type=Path, required=True)
    select.add_argument("--variant", required=True)
    select.add_argument("--note", default="")
    choose = sub.add_parser("choose-plan", help="Choose one reviewed composition for production before art")
    choose.add_argument("--run", type=Path, required=True)
    choose.add_argument("--variant", required=True)
    choose.add_argument("--note", required=True)
    choose.add_argument("--executor", required=True)
    publish = sub.add_parser("canon-publish", help="Publish an accepted collection entity for future runs")
    publish.add_argument("--project", type=Path, required=True)
    publish.add_argument("--run", type=Path, required=True)
    publish.add_argument("--entity", required=True)
    publish.add_argument("--new-version", action="store_true")
    for name in ('refine-start','refine-next','refine-status','refine-attach','refine-review'):
        r=sub.add_parser(name,help='Scored revision loop: threshold, strategy changes and three-edit discard')
        r.add_argument('--run',type=Path,required=True)
        if name=='refine-start':
            r.add_argument('--source',type=Path,required=True)
            r.add_argument('--audit',type=Path,required=True)
            r.add_argument('--allocation',type=Path,help='Reviewed category/star matrix; required if source has none')
        if name in ('refine-attach','refine-review'):r.add_argument('--job',required=True)
        if name=='refine-attach':
            r.add_argument('--image',type=Path,required=True)
            r.add_argument('--seconds',type=float)
        if name=='refine-review':r.add_argument('--response',type=Path,required=True)
    return p


def doctor(project):
    config, refs = check_project(project)
    from .typography import Lettering, project_profile
    lettering=Lettering(project_profile(project))
    checks = []
    for role, provider in config["providers"].items():
        kind = provider["type"]
        errors = []
        if kind == "codex" and not (provider.get("executable") or shutil.which("codex.cmd" if os.name == "nt" else "codex")):
            errors.append("Codex CLI not found")
        if kind in {"chat-completions", "image-cli"}:
            if not provider.get("model") or provider["model"].startswith("CHOOSE_"):
                errors.append("Choose an explicit model")
            key_env = provider.get("key_env", "OPENAI_API_KEY")
            if provider.get("require_key", True) and not os.environ.get(key_env):
                errors.append(f"Environment variable {key_env} is absent")
        if kind == "chat-completions" and not provider.get("base_url"):
            errors.append("Set base_url")
        if kind == "image-cli" and not find_spec("openai"):
            errors.append("Install the openai-image optional dependency")
        if kind == "command":
            executable = provider["argv"][0].replace("{python}", sys.executable)
            if not Path(executable).is_file() and not shutil.which(executable):
                errors.append("Command executable not found")
        if kind != "files" and provider.get("estimated_cost", 0) == 0:
            errors.append("Set a conservative nonzero estimated_cost, or explicitly mark zero_cost=true")
            if provider.get("zero_cost", False):
                errors.pop()
        checks.append({"role": role, "provider": kind, "ready": not errors,
                       "external_worker_required": kind == "files", "issues": errors})
    return {"ready": all(c["ready"] for c in checks), "reference_count": len(refs),
            "requires_external_workers": any(c["external_worker_required"] for c in checks),
            "providers": checks,"typography":lettering.public_profile()}


def status(pipeline):
    s = pipeline.state
    result = {"run": str(pipeline.run), "status": s["status"], "phase": s["phase"],
            "reason": s["reason"], "accepted_cards": len(s["accepted"]), "calls": s["calls"],
            "estimated_cost": s["estimated_cost"], "pending": s["pending"],
            "report": str(pipeline.run / "report.html"),
            'art_acceptance': s.get('art_acceptance'), 'art_stops': s.get('art_stops', {}),
            'allocation_policy': s.get('allocation_policy'), 'allocation_gate': s.get('allocation_gate'),
            'requested_variants': pipeline.brief.get('variants',3),
            'full_delivery_ready': s['status']=='complete' and len(s['accepted'])==pipeline.brief.get('variants',3)*10,
            'all_accepted': s['status'] == 'complete'}
    result.update(discarded_cards=s.get('discarded_cards',{}),discarded_sets=s.get('discarded_sets',{}),
                  qualified_entities=[e['id'] for e in s.get('canon',[])],reviewed_variants=s.get('reviewed_variants',[]))
    result['art_all_accepted']=s['status']=='complete'
    captions=pipeline.run/'nameplate-report.json'
    if captions.exists():
        result['nameplate_qa']=read(captions)['verdict']
        if s.get('nameplate_policy') or (pipeline.run/'presentation-naming-notes.json').exists():
            result['full_delivery_ready']=result['full_delivery_ready'] and result['nameplate_qa']=='pass'
    if 'orchestration' in s:
        from .workers import queue_status
        result.update(artists=s['orchestration']['artists'],
                      card_blockers=s['orchestration'].get('card_blockers', {}),
                      queue=queue_status(pipeline.run))
    return result


def coordinate(args):
    from .parallel import load
    from .workers import queued
    run, _ = queued(args.run)
    first = True
    while True:
        # Reload only after acquiring the writer lock. Long-running workers never
        # enter this lock and watch releases it between coordinator iterations.
        with lock_run(run):
            pipeline = load(run)
            if first and any(getattr(args,k,None) is not None for k in ('artists','reviewers','workers')):
                pipeline.set_capacity(artists=args.artists,reviewers=getattr(args,'reviewers',None),workers=getattr(args,'workers',None))
            before = sha(run / 'state.json')
            try:
                pipeline.advance()
            except NeedsInput:
                pass
            if sha(run / 'state.json') != before or not (run / 'report.html').exists():
                if pipeline.state['plan']:
                    contact_sheets(run, pipeline.state, pipeline.config)
                report(run, pipeline.state)
            result = status(pipeline)
        first = False
        terminal = result['status'] in ('complete', 'completed_with_rejections', 'needs_attention', 'waiting_for_selection')
        if not args.watch or terminal:
            output(result)
            return 0 if result['status'] == 'complete' else 3
        time.sleep(args.poll_seconds)


def queue_command(args):
    from .workers import queued, queue_status, public_job, work
    from .workqueue import WorkQueue
    run, _ = queued(args.run)
    if args.command == 'queue':
        output(queue_status(run))
        return 0
    if args.command == 'worker':
        result = work(run, args.worker, args.role, once=args.once,
                      poll_seconds=args.poll_seconds, lease_seconds=args.lease_seconds)
        output(result)
        return 3 if result['status'] in ('uncertain', 'lease_lost') else 0
    q = WorkQueue(run)
    if args.command == 'claim':
        result = q.claim(args.worker, args.role, args.lease_seconds)
        deadline=time.monotonic()+min(args.wait_seconds or 0,60)
        while result is None and time.monotonic()<deadline:
            if read(run/'state.json')['status'] in ('complete','completed_with_rejections'):break
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
            result=q.claim(args.worker,args.role,args.lease_seconds)
        output(public_job(result, lease=True) if result else
               {'status': 'idle', 'job': None, 'worker': args.worker,
                'role': args.role, 'order_complete': False})
        return 0
    if args.command == 'heartbeat':
        result = q.heartbeat(args.job, args.worker, args.token, args.lease_seconds)
    elif args.command == 'deliver':
        response=read(args.response)
        # Historical runs retain their original submission semantics.
        if read(run/'state.json')['orchestration'].get('version',1)>=2:
            from .preparation import preflight_response
            checked=preflight_response(run,args.job,response)
            if checked['verdict']!='pass':raise ValueError('Response preflight failed: '+json.dumps(checked,ensure_ascii=False))
        result = q.deliver(args.job, args.worker, args.token, response, args.model)
    elif args.command == 'abandon':
        result = q.abandon(args.job, args.worker, args.token, args.reason)
    else:
        result = q.settle_uncertain(args.job, args.reason)
    output(public_job(result))
    return 0


def publish_canon(args):
    from .parallel import load
    pipeline = load(args.run)
    if pipeline.state["status"] != "complete":
        raise ValueError("Publish canon only after whole-set acceptance.")
    source = next((e for e in pipeline.state["canon"]
                   if e["id"] == args.entity and e["scope"] == "collection"), None)
    if not source:
        raise ValueError("Collection entity not found.")
    project = args.project.resolve()
    config = read(project / "project.json")
    if config["collection_id"] != pipeline.config["collection_id"]:
        raise ValueError("Collection IDs differ.")
    with lock_run(project):
        canon = read(project / "canon.json")
        old = next((e for e in canon if e["id"] == args.entity), None)
        if old and not args.new_version:
            raise ValueError("Entity already has an active canon; use --new-version to publish a new version.")
        version = old["version"] + 1 if old else 1
        item = {k: source[k] for k in ("id", "scope", "description", "invariants", "allowed_changes")}
        item.update(version=version, images=[])
        if source.get('qualification')=='entity_only':
            item.update(qualification='entity_only',approved_components=source['approved_components'],
                        excluded_components=source['excluded_components'],
                        identity_origin={'artifact':source['identity_source_artifact'],
                                         'review_job':source['identity_review_job'],
                                         'input_manifest_sha256':sha(pipeline.run/'inputs/manifest.json')})
        for n, relative in enumerate(source["images"]):
            original = inside(pipeline.run, relative)
            dest = project / "canon" / args.entity / f"v{version}" / f"{n:02d}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError("Canon version path already exists.")
            shutil.copy2(original, dest)
            item["images"].append(dest.relative_to(project).as_posix())
        canon = [e for e in canon if e["id"] != args.entity] + [item]
        write(project / "canon.json", canon)
        event(project, "canon_published", entity=args.entity, version=version,
              source_manifest=sha(pipeline.run / "inputs/manifest.json"))
    output(item)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command=='font-configure':
            from .typography import configure
            output(configure(args.run or args.project,args.font,run=bool(args.run),neutral=args.neutral));return 0
        if args.command == 'import-style':
            from .stylepack import configure
            output(configure(args.project,args.style_pack));return 0
        if args.command in ('generators','probe-generators'):
            from .generators import discover,probe_plan
            output(discover(args.project,args.capabilities) if args.command=='generators' else
                   probe_plan(args.project,args.out,args.capabilities));return 0
        if args.command == 'inbox':
            from .ui import orders
            deadline=time.monotonic()+min(args.wait_seconds or 0,60)
            while True:
                pending=[x for x in orders(args.runs) if x['status'] not in ('complete','completed_with_rejections')]
                if pending or time.monotonic()>=deadline:break
                time.sleep(min(.5,max(0,deadline-time.monotonic())))
            output({'orders':pending,'external_agent_required':True});return 0
        if args.command in ('export','result-manifest'):
            from .delivery import save_manifest,export_bundle
            if args.command=='result-manifest':
                result=save_manifest(args.run);output(result)
                return 0 if result['full_delivery_ready'] else 3
            result=export_bundle(args.run,args.out)
            from .showcase import render
            render(args.run,read(args.run/'state.json'),read(args.run/'inputs/brief.json'))
            output(result);return 0
        if args.command == 'ui':
            from .ui import serve
            serve(args.project,args.runs or args.project.parent/'runs',args.port,artists=args.artists,reviewers=args.reviewers,workers=args.workers)
            return 0
        if args.command == 'coordinate':
            return coordinate(args)
        if args.command == 'revise-card':
            from .parallel import load
            from .revision import revise_card
            with lock_run(args.run):
                output(revise_card(load(args.run),args.card,args.note))
            return 0
        if args.command in {'queue','claim','heartbeat','deliver','abandon','reconcile','worker'}:
            return queue_command(args)
        if args.command in ('image-start', 'image-finish'):
            from .image_execution import start as start_image, finish as finish_image
            if args.command == 'image-start':
                output(start_image(args.run, args.job, args.worker, args.token, tool=args.tool,
                                   model=args.model, max_images=args.max_images, prompt_file=args.prompt_file))
                return 0
            result = finish_image(args.run, args.job, args.worker, args.token,
                                  image=args.image, error=args.error, note=args.note)
            output(result)
            return 0 if result['status'] == 'succeeded' else 2
        if args.command in ('validate-response','prepare-job','reference-cache'):
            from .preparation import preflight_response,InspectedReferenceCache,framing_guidance
            run=args.run.resolve();request=read(inside(run,'jobs/'+args.job+'/request.json'))
            if args.command=='validate-response':
                checked=preflight_response(run,args.job,read(args.response));output(checked)
                return 0 if checked['verdict']=='pass' else 2
            cache=InspectedReferenceCache(run/'worker-cache'/(hashlib.sha256(args.worker.encode()).hexdigest()+'.sqlite3'),args.worker)
            images=request.get('images',[])
            if args.command=='reference-cache' and args.mark_path:
                if not args.role or not args.sha256 or not args.evidence:raise ValueError('Mark requires role, sha256 and visual evidence.')
                image=next((x for x in images if x['path']==args.mark_path and x['role']==args.role),None)
                if image is None:raise ValueError('Reference is not in the exact job request.')
                output(cache.mark_inspected(run,image,evidence=args.evidence,expected_sha256=args.sha256));return 0
            result={'job':args.job,'worker':args.worker,'uninspected':cache.uninspected(run,images)}
            if args.command=='prepare-job':
                result.update(request=request,framing_guidance=framing_guidance())
                if request.get('kind') == 'image':
                    from .image_execution import image_call
                    result['image_call'] = image_call(run, request)
            output(result);return 0
        if args.command == 'brief':
            from .schemas import BRIEF, validate
            value={'theme':args.theme,'wishes':args.wishes,'variants':args.variants}
            validate(value,BRIEF)
            if not value['theme'].strip():raise ValueError('Theme is required.')
            if args.out.exists():raise ValueError('Brief already exists; choose a new filename.')
            write(args.out,value)
            output({'brief':str(args.out.resolve()),**value})
            return 0
        if args.command == 'acceptance-policy':
            if not args.run:
                output(art_policy())
            elif (args.run / 'inputs/acceptance-policy.json').exists():
                output(read(args.run/'state.json')['art_acceptance'])
            elif (args.run / 'policy.json').exists():
                output(read(args.run / 'policy.json'))
            else:
                output({'active': False, 'reason': 'Historical run has no scored acceptance policy; unchanged.'})
            return 0
        if args.command=='nameplate-check':
            import copy
            from .nameplate import preflight,DEFAULT_POLICY
            from .typography import for_run, Lettering, project_profile
            from .naming import names,display_title
            policy=dict(DEFAULT_POLICY)
            if args.run:
                if args.project:raise ValueError('--project applies only to --plan, not --run.')
                lettering=for_run(args.run)
                state=read(args.run/'state.json');plan=copy.deepcopy(state.get('plan') or {'variants':[]})
                policy.update(state.get('nameplate_policy',{}));mapping=names(args.run)
                for v in plan['variants']:
                    for c in v['cards']:c['title']=display_title(mapping,v,c)
                    v['title']=display_title(mapping,v)
                notes=args.run/'presentation-naming-notes.json'
                if notes.exists():policy['producer_flagged_labels']=read(notes).get('producer_flagged_labels',[])
            else:
                plan=read(args.plan)
                lettering=Lettering(project_profile(args.project)) if args.project else Lettering()
            result=preflight(plan,policy,lettering.measure_label)
            result['typography']=lettering.public_profile()
            if args.out:write(args.out,result)
            output(result);return 0 if result['verdict']=='pass' else 2
        if args.command.startswith('refine-'):
            from .refine import execute
            result = execute(args)
            output(result)
            return 3 if result.get('status') == 'stopped_unaccepted' else 0
        if args.command == "init":
            init_project(args.project, args.materials)
            output({"project": str(args.project.resolve()), "status": "initialized"})
            return 0
        if args.command == "doctor":
            result = doctor(args.project)
            output(result)
            return 0 if result["ready"] else 2
        if args.command == "schema":
            schemas = {**SCHEMAS, **({"plan": PLAN_V2, "review": REVIEW_V2} if args.contract_version == 2 else {})}
            schemas['card-review'] = scored_card_review(args.contract_version)
            schemas['production-plan'] = production_plan(args.contract_version)
            schemas['production-card-review'] = scored_card_review(args.contract_version, allocated=True)
            output(schemas[args.name])
            return 0
        if args.command == "canon-publish":
            publish_canon(args)
            return 0
        if args.command == "unlock":
            lock = args.run.resolve() / ".lock"
            if lock.exists():
                # Explicit recovery command; do not infer that a remote PID is dead.
                lock.unlink()
            output({"status": "unlocked"})
            return 0
        if args.command == "run":
            value = read(args.brief) if args.brief else {'theme':args.theme}
            if args.wishes is not None:value['wishes']=args.wishes
            if args.variants is not None:value['variants']=args.variants
            if args.mode is not None:value['production_mode']=args.mode
            pipeline = Pipeline.create(args.project, value, args.run, artists=args.artists,reviewers=args.reviewers,workers=args.workers)
        else:
            from .parallel import load
            pipeline = load(args.run)
        with lock_run(pipeline.run):
            # A second short-lived coordinator may have advanced it before the
            # lock was acquired. Never apply a stale state object.
            from .parallel import load
            pipeline = load(pipeline.run)
            try:
                if args.command == "submit":
                    if 'orchestration' in pipeline.state:
                        if args.job not in {j['id'] for j in pipeline.state['jobs']}:
                            raise ValueError('Job ID does not match an issued queue request.')
                        raise ValueError('Queued jobs require claim/deliver with the lease token; submit cannot bypass ownership.')
                    pipeline.check_snapshot()
                    if not pipeline.state["pending"] or pipeline.state["pending"]["id"] != args.job:
                        raise ValueError("Job ID does not match the pending request.")
                    pipeline.accept(read(args.response), args.executor, args.model)
                elif args.command == "fail":
                    if 'orchestration' in pipeline.state:
                        raise ValueError('Queued jobs require abandon with the lease token; fail cannot bypass ownership.')
                    if not pipeline.state["pending"] or pipeline.state["pending"]["id"] != args.job:
                        raise ValueError("Job ID does not match the pending request.")
                    pipeline.provider_failure(ValueError(args.reason))
                elif args.command == "continue":
                    pipeline.resume_attention(args.note, {
                        "max_calls": args.extra_calls, "max_attempts_per_task": args.extra_attempts,
                        "max_set_revisions": args.extra_set_revisions,
                        "max_estimated_cost": args.extra_estimated_cost})
                elif args.command == 'amend-policy':
                    pipeline.amend_policy(args.note)
                elif args.command == "select":
                    if pipeline.state["status"] != "complete":
                        raise ValueError("Choose a variant after all production gates pass.")
                    if args.variant not in {v["id"] for v in pipeline.state["plan"]["variants"]}:
                        raise ValueError("Unknown variant ID.")
                    selection = {"variant_id": args.variant, "note": args.note}
                    pipeline.state["selection"] = selection
                    write(pipeline.run / "selection.json", selection)
                    event(pipeline.run, "variant_selected", **selection)
                    pipeline.save()
                elif args.command == "choose-plan":
                    pipeline.choose_plan(args.variant, args.note, args.executor)
                elif args.command == 'allocation-check':
                    if not pipeline.allocated:
                        raise ValueError('Historical run has no approved pre-art allocation. Use an explicit metadata amendment.')
                    require_gate(pipeline.state)
                    if sha(pipeline.run/'allocation.json') != pipeline.state['allocation_gate']['file_sha256']:
                        raise ValueError('Approved allocation artifact changed.')
                    output(check_allocation(pipeline.state['plan'], pipeline.state['allocation_policy']))
                    return 0
                if args.command in {"run", "resume", "submit", "continue", "fail", "choose-plan"}:
                    pipeline.advance()
                elif args.command == "pack":
                    target = args.out.resolve()
                    if target.exists():
                        raise ValueError("Archive already exists; choose a new output name.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='setforge-pack-') as tmp:
                        database = None
                        if 'orchestration' in pipeline.state:
                            from .workqueue import WorkQueue
                            database = WorkQueue(pipeline.run).backup(Path(tmp)/'queue.sqlite3')
                        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                            for path in pipeline.run.rglob("*"):
                                skip = path.name == '.lock' or (database is not None and path.name in ('queue.sqlite3','queue.sqlite3-wal','queue.sqlite3-shm'))
                                if path.is_file() and path.resolve() != target and not skip:
                                    archive.write(path, path.relative_to(pipeline.run).as_posix())
                            if database is not None:
                                archive.write(database,'queue.sqlite3')
            except NeedsInput as exc:
                print(str(exc), file=sys.stderr)
                output(status(pipeline))
                return 3
            finally:
                if args.command != 'pack':
                    if pipeline.state["plan"]:
                        contact_sheets(pipeline.run, pipeline.state, pipeline.config)
                    report(pipeline.run, pipeline.state)
            output(status(pipeline))
            return 0
    except KeyboardInterrupt:
        output({'status': 'interrupted', 'run': str(getattr(args, 'run', ''))})
        return 130
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
