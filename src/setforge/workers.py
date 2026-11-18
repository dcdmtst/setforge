"""Replaceable provider workers. No production-state writes or run-wide lock."""
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from .io import inside, read, sha, write
from .providers import invoke
from .workqueue import WorkQueue, ROLES,CLAIM_ROLES


def queued(run):
    run = Path(run).resolve()
    state = read(run / 'state.json')
    if 'orchestration' not in state:
        raise ValueError('This is a historical serial run; use its submit/fail protocol.')
    return run, state


def public_job(job, *, lease=False):
    if job is None:
        return None
    keys = ('id', 'role', 'kind', 'card', 'task_key', 'status', 'worker','priority',
            'expires_at', 'lease_attempt', 'error', 'uncertain_reason', 'cancel_reason')
    result = {key: job.get(key) for key in keys if key in job}
    result.update(request=f"jobs/{job['id']}/request.json", schema=f"jobs/{job['id']}/schema.json")
    if lease:
        result['token'] = job['token']
        result['request_sha256'] = job['request_sha256']
    return result


def queue_status(run):
    run, state = queued(run)
    jobs = WorkQueue(run).jobs()
    counts = {}
    for item in jobs:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    return {'run': str(run), 'artists': state['orchestration']['artists'],
            'reviewers':state['orchestration'].get('reviewers',1), 'workers':state['orchestration'].get('workers'), 'planners': 1, 'counts': counts,
            'active': [public_job(j) for j in jobs if j['status'] in ('leased', 'submitted', 'uncertain')],
            'jobs': [public_job(j) for j in jobs]}


def work_once(run, worker, role, lease_seconds=900):
    run, state = queued(run)
    if role not in CLAIM_ROLES:
        raise ValueError('Unknown worker role.')
    config = read(run / 'inputs/project.json')
    candidates=[config['providers'][r] for r in ('image','reviewer')] if role=='flex' else [config['providers'][role]]
    if any(provider['type']=='files' for provider in candidates):
        raise ValueError('The files provider needs an external agent: claim the job, perform it, then deliver its JSON. No job was claimed.')
    # The coordinator persists its application/issue context before projecting
    # a local stop back into the shared queue. That intermediate needs_attention
    # is not a signal for independent workers to terminate.
    transient = state.get('_queue_applying') or state.get('_queue_issuing')
    if state['status'] in ('complete','completed_with_rejections') or (state['status'] == 'needs_attention' and not transient):
        return {'status': 'terminal', 'run_status': state['status'], 'worker': worker, 'role': role}
    q = WorkQueue(run)
    lease = q.claim(worker, role, lease_seconds)
    if lease is None:
        return {'status': 'idle', 'worker': worker, 'role': role, 'order_complete': False}
    actual_role=lease['role'];provider=config['providers'][actual_role]
    job_id, token = lease['id'], lease['token']
    folder = inside(run, f'jobs/{job_id}')
    # Every lease owns an execution folder. Reassigned review/planning work must
    # never read or overwrite a stale worker's response file.
    execution = folder / 'executions' / hashlib.sha256(token.encode()).hexdigest()[:20]
    execution.mkdir(parents=True, exist_ok=True)
    write(execution / 'lease.json', {'job': job_id, 'worker': worker, 'token': token,
                                   'role': role, 'expires_at': lease['expires_at']})
    stop = threading.Event()
    heartbeat_errors = []

    def heartbeat():
        interval = max(.001, min(30, lease_seconds / 3))
        while not stop.wait(interval):
            try:
                q.heartbeat(job_id, worker, token, lease_seconds)
            except (ValueError, OSError, sqlite3.Error) as exc:
                heartbeat_errors.append(str(exc))
                break

    thread = threading.Thread(target=heartbeat, daemon=True)
    started = False
    try:
        if sha(folder / 'request.json') != lease['request_sha256']:
            raise ValueError('Claimed request differs from its immutable queue hash.')
        request = read(folder / 'request.json')
        if actual_role=='image' and state['orchestration'].get('version',1)>=2:
            from .preparation import framing_guidance
            request={**request,'instruction':request['instruction']+'\n'+framing_guidance()}
        write(execution / 'request.json', request)
        write(execution / 'schema.json', request['output_schema'])
        thread.start()
        started = True
        response = invoke(provider, request, run, execution, config['limits']['timeout_seconds'])
        if not isinstance(response, dict):
            raise ValueError('Provider did not return a JSON object.')
        # This durable file survives a crash between provider completion and inbox
        # delivery. Recover it explicitly with the original lease, not a new call.
        write(execution / 'worker-response.json', response)
        if state['orchestration'].get('version',1)>=2:
            from .preparation import preflight_response
            checked=preflight_response(run,job_id,response)
            write(execution/'response-preflight.json',checked)
            if checked['verdict']!='pass':raise ValueError('Response preflight failed: '+str(checked['errors']))
        result = q.deliver(job_id, worker, token, response, provider.get('model', 'unspecified'))
        return {'status': 'delivered', 'job': job_id, 'worker': worker, 'role':actual_role,
                'response': str(execution / 'worker-response.json'),
                'queue_status': result['status'], 'heartbeat_warnings': heartbeat_errors}
    except Exception as exc:
        reason = str(exc)
        write(execution / 'worker-error.json', {'error': reason, 'provider_started': started,
                                               'heartbeat_warnings': heartbeat_errors})
        try:
            if actual_role == 'image' and started:
                result = q.quarantine(job_id, worker, token, reason)
            else:
                result = q.abandon(job_id, worker, token, reason)
            outcome = result['status']
        except (ValueError, sqlite3.Error):
            # A lost lease must not let its old worker mutate a replacement.
            outcome = 'lease_lost'
        return {'status': outcome, 'job': job_id, 'worker': worker, 'role': role,
                'error': reason, 'execution': str(execution)}
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=1)


def work(run, worker, role, *, once=False, poll_seconds=1, lease_seconds=900, stop_event=None):
    """Run a provider loop; an external coordinator must process its inbox."""
    results = []
    while stop_event is None or not stop_event.is_set():
        result = work_once(run, worker, role, lease_seconds)
        if once or result['status'] in ('terminal', 'uncertain', 'lease_lost'):
            return result
        if result['status'] != 'idle':
            results.append({key: value for key, value in result.items() if key != 'response'})
        if stop_event is not None:
            stop_event.wait(poll_seconds)
        else:
            time.sleep(poll_seconds)
    return {'status': 'stopped', 'worker': worker, 'role': role, 'processed': len(results)}
