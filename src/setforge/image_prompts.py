"""Faithful, size-bounded tool prompts; full immutable requests stay untouched."""
import json
import re
from pathlib import Path

from .io import read


IMAGEGEN_PROMPT_LIMIT = 28000  # Leave headroom below the provider's 32,000 ceiling.
_ADMIN_CONTEXT = {
    'art_history', 'art_acceptance', 'nameplate_policy', 'allocation_policy',
    'production_contract', 'accepted_images', 'construction_targets',
    'composition_options', 'reference_catalog', 'plan', 'execution_error',
}


def validate_prompt(prompt, tool='imagegen'):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Exact tool prompt cannot be blank.')
    limit = IMAGEGEN_PROMPT_LIMIT if tool == 'imagegen' else None
    if limit and len(prompt) > limit:
        raise ValueError(f'Image prompt has {len(prompt)} characters; safe imagegen limit is {limit}. '
                         'No call/timer was started. Shorten redundant wording in the exact prompt, '
                         'preserving wishes, current card, canon, input roles and targeted fixes; '
                         'never truncate requirements or silently retry.')
    return limit


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _technical(feedback, run):
    if not isinstance(feedback, str) or not feedback.strip():
        return False
    if re.search(r'HTTP\s*\d{3}|prompt.{0,30}(?:too long|32000|32,000)|rate.?limit|quota exhausted|provider.{0,20}(?:error|failure)', feedback, re.I):
        return True
    for path in (Path(run) / 'jobs').glob('*-image/generation-record.json'):
        record = read(path)
        if record.get('status') == 'failed' and record.get('error') == feedback:
            return True
    return False


def targeted_feedback(run, request):
    """Recover old overwritten direction only for the exact same card candidate."""
    feedback = request.get('feedback', '')
    source = request.get('job_id')
    if _technical(feedback, run):
        context = request.get('context', {})
        card = context.get('card', {})
        identity = tuple(card.get(k) for k in ('id', 'subject', 'scene'))
        current_sequence = int(source.split('-', 1)[0]) if source else float('inf')
        previous = []
        for path in (Path(run) / 'jobs').glob('*-image/request.json'):
            try:
                sequence = int(path.parent.name.split('-', 1)[0])
            except ValueError:
                continue
            if sequence >= current_sequence:
                continue
            previous.append((sequence, path))
        feedback, source = '', None
        for _, path in sorted(previous, reverse=True):
            prior = read(path)
            other = prior.get('context', {})
            same_card = tuple(other.get('card', {}).get(k) for k in ('id', 'subject', 'scene')) == identity
            if (prior.get('task_key') == request.get('task_key') and same_card
                    and other.get('card_candidate') == context.get('card_candidate')
                    and prior.get('feedback') and not _technical(prior['feedback'], run)):
                feedback, source = prior['feedback'], prior.get('job_id', path.parent.name)
                break
    key = request.get('task_key', '').removeprefix('image:')
    if isinstance(feedback, dict):
        # Scores, whole-set summaries and unrelated cards are review data, not a
        # generation brief. Keep exact actionable strings, never summarise them.
        excluded = {'criteria', 'verdict', 'action', 'affected_cards', 'findings',
                    'summary', 'art_review', 'construction_review', 'construction_rechecks'}
        useful = {k: v for k, v in feedback.items() if k not in excluded}
        findings = [f for f in feedback.get('findings', [])
                    if not f.get('targets') or key in f['targets']]
        if findings:
            useful['findings'] = findings
        art = feedback.get('art_review', {})
        direction = {k: art[k] for k in ('feedback', 'next_strategy') if art.get(k)}
        if direction:
            useful['art_direction'] = direction
        if feedback.get('construction_review'):
            useful['construction_review'] = feedback['construction_review']
        rechecks = [r for r in feedback.get('construction_rechecks', []) if r.get('card') == key]
        if rechecks:
            useful['construction_rechecks'] = rechecks
        if not useful:
            useful['criteria'] = [c for c in feedback.get('criteria', []) if c.get('verdict') != 'pass']
            if not useful['criteria'] and feedback.get('summary'):
                useful['summary'] = feedback['summary']
        feedback = useful
    return feedback, source


def build(run, request, selected, aliases, *, tool='imagegen'):
    context = request.get('context', {})
    feedback, feedback_source = targeted_feedback(run, request)
    if context.get('card'):
        # Preserve all current-card and unknown/custom constraint fields. Exclude
        # only explicit orchestration/history data; style is emitted exactly once.
        compact = {k: v for k, v in context.items() if k not in _ADMIN_CONTEXT | {'style'}}
        if 'variant' in compact:
            compact['variant'] = {k: v for k, v in compact['variant'].items() if k != 'entities'}
        instruction = request.get('instruction', '').replace(
            'Return image_path relative to run root (or an absolute local path), plus note.', '')
        prompt = (instruction + '\n\nSTYLE\n' + context.get('style', '')
                  + '\n\nCURRENT ART BRIEF\n' + _dump(compact))
        # Engine-added physical guidance is outside context in historical jobs.
        marker = '\n\nPHYSICAL CONSTRUCTION\n'
        if marker in request.get('prompt', ''):
            prompt += marker + request['prompt'].split(marker, 1)[1]
    else:
        # Historic/custom minimal requests have no structured card to render.
        prompt = request.get('prompt', '')
        marker = '\n\nINPUT IMAGES IN ORDER\n'
        if marker in prompt:
            prompt = prompt.split(marker, 1)[0]
    slots = []
    for index, item in enumerate(selected, 1):
        roles = [item['role']]
        for group in aliases:
            if group['image']['sha256'] == item['sha256']:
                roles += [a['role'] for a in group['aliases']]
        slots.append({'input': index, 'path': item['path'], 'roles': roles})
    prompt += '\n\nINPUT IMAGES IN ORDER\n' + _dump(slots)
    prompt += ('\nOriginal style inputs define rendering only. Canon inputs define ONLY approved identity '
               'components/invariants; do not copy their old poses, backgrounds or excluded props. '
               'The edit target defines the image being corrected, never a new style reference.')
    prompt += '\n\nTARGETED FEEDBACK\n' + (feedback if isinstance(feedback, str) else _dump(feedback))
    from .preparation import framing_guidance
    prompt += '\n\n' + framing_guidance()
    limit = validate_prompt(prompt, tool)
    return prompt, {'policy': 'current-card-faithful-v1', 'characters': len(prompt),
                    'max_characters': limit, 'immutable_prompt_characters': len(request.get('prompt', '')),
                    'feedback_source_job': feedback_source,
                    'excluded_context_fields': sorted(set(context) & _ADMIN_CONTEXT),
                    'full_request_retained': True}
