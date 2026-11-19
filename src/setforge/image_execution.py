"""Bounded image-tool inputs and machine-written attempt receipts.

These helpers never generate art, accept a card, or advance production state.
The immutable job keeps the full reference list for visual inspection/review.
"""

import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .io import inside, locked, read, sha, write, now, redact


def job_request(run, job_id):
    run = Path(run).resolve()
    if not re.fullmatch(r'[0-9]+-image', job_id):
        raise ValueError('Use the exact image job ID.')
    folder = inside(run, 'jobs/' + job_id)
    request = read(folder / 'request.json')
    if request.get('kind') != 'image' or request.get('job_id') != job_id:
        raise ValueError('Image attempt does not match the immutable job request.')
    return run, folder, request


def image_call(run, request, *, max_images=None, tool='imagegen', prompt_override=None):
    """Select exact inputs before a call; mandatory identities are never dropped."""
    run = Path(run).resolve()
    if request.get('kind') != 'image':
        raise ValueError('Image input selection is only for generation, never review.')
    provider = read(run / 'inputs/project.json')['providers']['image']
    configured = provider.get('max_images', 5 if provider['type'] == 'files' else 32)
    if type(configured) is not int or configured < 1:
        raise ValueError('Image provider max_images must be a positive integer.')
    if max_images is not None and (type(max_images) is not int or max_images < 1):
        raise ValueError('Tool image limit must be a positive integer.')
    # Builtin imagegen has a known ceiling even if project config overstates it.
    limit = min(configured, max_images if max_images is not None else configured)
    if tool == 'imagegen':
        limit = min(limit, 5)
    records = []
    for item in request.get('images', []):
        path = inside(run, item['path'])
        digest = sha(path)
        if item.get('sha256') and item['sha256'] != digest:
            raise ValueError('Changed input: ' + item['path'])
        role = item['role']
        if not role.startswith(('canon:', 'edit_target:', 'original_style_reference:')):
            raise ValueError('Unexpected generation input role: ' + role)
        records.append({**item, 'sha256': digest})
    card_ids = request.get('context', {}).get('card', {}).get('reference_ids', [])
    def priority(item):
        role = item['role']
        if role.startswith('edit_target:'):return 0
        if role.startswith('canon:'):return 1
        return 2 if role.split(':', 1)[1] in card_ids else 3
    groups = []
    by_hash = {}
    for item in sorted(records, key=priority):
        if item['sha256'] in by_hash:
            by_hash[item['sha256']]['aliases'].append(item)
        else:
            group = {'image': item, 'aliases': []}
            by_hash[item['sha256']] = group
            groups.append(group)
    mandatory = [g for g in groups if priority(g['image']) < 2]
    styles = [g for g in groups if priority(g['image']) >= 2]
    if len(mandatory) + bool(styles) > limit:
        raise ValueError('Mandatory canon/edit targets plus an original style reference exceed the tool limit. '
                         'Do not call the model or drop canon. Reduce identity source views upstream or use a capable executor.')
    selected = mandatory + styles[:max(0, limit - len(mandatory))]
    chosen = [g['image'] for g in selected]
    selected_hashes = {i['sha256'] for i in chosen}
    omitted = []
    for group in groups:
        if group['image']['sha256'] not in selected_hashes:
            omitted.append({**group['image'], 'reason': 'tool_limit; lower-priority original style reference'})
        for alias in group['aliases']:
            omitted.append({**alias, 'reason': 'same bytes as another input; role retained in aliases'})
    aliases = [g for g in selected if g['aliases']]
    from .image_prompts import build, validate_prompt
    if prompt_override is None:
        prompt, prompt_policy = build(run, request, chosen, aliases, tool=tool)
    else:
        limit = validate_prompt(prompt_override, tool)
        prompt = prompt_override
        prompt_policy = {'policy': 'exact-worker-override', 'characters': len(prompt),
                         'max_characters': limit, 'full_request_retained': True}
    return {'schema_version': 1, 'max_images': limit,
            'selection_policy': 'edit target, every canon source, card-specific originals, global originals; deduplicate bytes',
            'inspection_images': records, 'selected': chosen, 'omitted': omitted,
            'aliases': aliases, 'prompt_policy': prompt_policy,
            'prompt': prompt,
            'referenced_image_paths': [str(inside(run, i['path'])) for i in chosen],
            'request_sha256': sha(inside(run, 'jobs/' + request['job_id'] + '/request.json'))}


def _owner(run, folder, job_id, worker, token, *, starting):
    from .workqueue import WorkQueue
    job = next((j for j in WorkQueue(run).jobs() if j['id'] == job_id), None)
    if not job or job['worker'] != worker or job['token'] != token:
        raise ValueError('Image attempt requires the exact current worker lease.')
    allowed = ('leased',) if starting else ('leased', 'uncertain')
    if job['status'] not in allowed or (starting and job['expires_at'] <= time.time()):
        raise ValueError('Lease cannot start/finish an image call in this state; recover the recorded attempt.')
    if job['request_sha256'] != sha(folder / 'request.json'):
        raise ValueError('Immutable request hash changed.')


def start(run, job_id, worker, token, *, tool='imagegen', model='unspecified', max_images=None, prompt_file=None):
    run, folder, request = job_request(run, job_id)
    with locked(folder):
        _owner(run, folder, job_id, worker, token, starting=True)
        if (folder / 'generation-record.json').exists():
            raise ValueError('An image attempt is already recorded for this job. Recover it; do not generate again.')
        exact_prompt = Path(prompt_file).read_text(encoding='utf-8-sig') if prompt_file else None
        call = image_call(run, request, max_images=max_images, tool=tool, prompt_override=exact_prompt)
        write(folder / 'generation-inputs.json', {k: v for k, v in call.items() if k != 'prompt'})
        (folder / 'generation-prompt.txt').write_text(call['prompt'], encoding='utf-8')
        record = {'schema_version': 1, 'status': 'started', 'job_id': job_id,
                  'worker': worker, 'tool': tool, 'model': model,
                  'request_sha256': call['request_sha256'],
                  'prompt_sha256': sha(folder / 'generation-prompt.txt'),
                  'inputs_sha256': sha(folder / 'generation-inputs.json'),
                  'cash_spend': 0, 'started_at': now(),
                  'timing_scope': 'executor call envelope; includes dispatch/return overhead, not pure model compute'}
        write(folder / 'generation-record.json', record)
    return {'job_id': job_id, 'prompt': call['prompt'],
            'referenced_image_paths': call['referenced_image_paths'],
            'transparent_background': False, 'started_at': record['started_at'],
            'receipt': str(folder / 'generation-record.json')}


def finish(run, job_id, worker, token, *, image=None, error=None, note='Generated PNG; metadata recorded by Setforge.'):
    # Capture completion before hashing/copying so those operations aren't model time.
    ended_at = now()
    run, folder, request = job_request(run, job_id)
    with locked(folder):
        _owner(run, folder, job_id, worker, token, starting=False)
        record = read(folder / 'generation-record.json')
        if record['worker'] != worker or record['status'] != 'started':
            raise ValueError('Only the owner can finish an open recorded attempt; closed receipts are immutable.')
        if record['request_sha256'] != sha(folder / 'request.json'):
            raise ValueError('Request changed during generation.')
        seconds = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(record['started_at'])).total_seconds()
        if seconds < 0:raise ValueError('Clock moved backwards; do not invent timing.')
        record.update(ended_at=ended_at, received=ended_at, seconds=seconds)
        if error is not None:
            record.update(status='failed', error=redact(error))
        else:
            try:
                source = Path(image).expanduser()
                if not source.is_absolute():source = inside(run, source)
                source = source.resolve()
                from PIL import Image
                with Image.open(source) as png:
                    if png.format != 'PNG':raise ValueError('Tool result must be its genuine PNG.')
                    size = list(png.size)
                    png.verify()
                source_digest = sha(source)
                target = inside(run, request['suggested_output_path'])
                if target.exists() and target.resolve() != source and sha(target) != source_digest:
                    raise ValueError('Output already exists; recover it without overwriting.')
                if target.resolve() != source and not target.exists():shutil.copy2(source, target)
                if sha(target) != source_digest:
                    raise ValueError('PNG bytes changed while preserving the tool output.')
                record.update(status='succeeded', source=str(source), sha256=source_digest,
                              image_path=target.relative_to(run).as_posix(), size=size)
                response = {'image_path': record['image_path'], 'note': note}
                write(folder / 'image-response.json', response)
            except (ValueError, OSError) as exc:
                record.update(status='failed', error=redact(str(exc)))
        write(folder / 'generation-record.json', record)
        write(folder / 'generation-tool-timing.json', {k: record[k] for k in
              ('job_id', 'worker', 'tool', 'started_at', 'ended_at', 'seconds', 'timing_scope')})
    return {'job_id': job_id, 'status': record['status'], 'seconds': seconds,
            'response': str(folder / 'image-response.json') if record['status'] == 'succeeded' else None,
            'sha256': record.get('sha256'), 'error': record.get('error')}
