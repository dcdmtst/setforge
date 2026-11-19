"""Bounded, independently reviewed replacement concepts; histories are never reset."""
import copy
from .io import event, now, write, sha


def policy():
    return {'version': 1, 'max_candidates_per_slot': 3,
            'meaning': 'Initial concept plus at most two genuinely different replacement concepts; each retains its own initial image and three edits.'}


def number(state, key):
    return state.get('card_candidates', {}).get(key, {}).get('number', 1)


def exhausted(pipeline, key, reason):
    state = pipeline.state
    if not state.get('replacement_policy'):
        return False
    current = state.setdefault('card_candidates', {}).setdefault(key, {'number': 1, 'history': []})
    if not current['history'] or current['history'][-1]['number'] != current['number']:
        current['history'].append({'number': current['number'], 'card': copy.deepcopy(pipeline.find(key)[1]),
                                   'reason': reason, 'time': now(),
                                   'artifacts': [copy.deepcopy(h) for h in state['art_history'].get(key, [])
                                                 if h.get('candidate', 1) == current['number']]})
    if current['number'] >= state['replacement_policy']['max_candidates_per_slot']:
        return False
    state['accepted'].pop(key, None)
    state.setdefault('replacement_pending', {})[key] = {'reason': reason, 'next_candidate': current['number'] + 1}
    state['art_stops'][key] = 'replacement_pending'
    state.get('orchestration', {}).get('card_blockers', {}).pop(key, None)
    event(pipeline.run, 'card_replacement_required', card=key, exhausted_candidate=current['number'], reason=reason)
    return True


def proposed_plan(pipeline, card):
    key = pipeline.state['replacement_current']
    old = pipeline.find(key)[1]
    for field in ('id', 'background', 'stars', 'golden', 'key_character_count', 'entity_ids'):
        if card.get(field) != old.get(field):
            raise ValueError('Replacement must preserve the slot allocation and identity dependencies: ' + field)
    prior = pipeline.state['card_candidates'][key]['history']
    for attempt in prior:
        previous = attempt['card']
        if (card['concept']['canonical_subject'] == previous['concept']['canonical_subject']
                or card['subject'].strip().casefold() == previous['subject'].strip().casefold()
                or card['scene'].strip().casefold() == previous['scene'].strip().casefold()):
            raise ValueError('Replacement must be a genuinely new subject/action, not the same failed card renamed.')
    result = copy.deepcopy(pipeline.state['plan'])
    for v in result['variants']:
        for i, c in enumerate(v['cards']):
            if v['id'] + '/' + c['id'] == key:v['cards'][i] = copy.deepcopy(card)
    pipeline.validate_plan(result)
    return result


def commit(pipeline):
    state = pipeline.state; key = state['replacement_current']
    plan = proposed_plan(pipeline, state['replacement_proposal'])
    current = state['card_candidates'][key]
    current['number'] += 1
    state['plan'] = plan
    state['latest'].pop(key, None)
    state['art_stops'].pop(key, None)
    state['feedback'].pop('image:' + key, None)
    state.setdefault('execution_errors', {}).pop('image:' + key, None)
    state['replacement_pending'].pop(key)
    state.pop('replacement_proposal', None); state.pop('replacement_current', None)
    # Recheck the whole allocation/name/staging matrix before the replacement's first image.
    from .allocation import check
    gate = check(plan, state['allocation_policy'])
    write(pipeline.run/'allocation.json', gate)
    state['allocation_gate'] = {k: gate[k] for k in ('verdict', 'plan_sha256')}
    state['allocation_gate']['file_sha256'] = sha(pipeline.run/'allocation.json')
    write(pipeline.run/'plans.json', plan)
    write(pipeline.run/'replacements'/key.replace('/', '--')/f'candidate-{current["number"]}.json',
          {'plan': plan, 'candidate': current['number'], 'time': now()})
    event(pipeline.run, 'card_replacement_approved', card=key, candidate=current['number'])
    state.update(phase='next_card', current=None)
