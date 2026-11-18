"""Evidence contract for physical assemblies; visual judgement stays independent."""
from .io import inside, sha


GUIDANCE = (
    'Before beauty, narrative and scores inspect the focal physical construction. '
    'Identify whole objects and their parts, trace actual joints/contact surfaces, '
    'support, perspective and the actor interaction. For lids/doors/drawers trace '
    'the axis or travel, matching opening and plausible closed position; existence '
    'of two hinges does not prove that a lid belongs to the furniture. Inspect '
    'risky junctions close up at native resolution, then the thumbnail. Do not '
    'invent hidden braces to explain visible contradictions or call an unexplained '
    'assembly coherent. Stylized proportions, explicit fantasy and natural '
    'occlusion are allowed; photorealistic engineering and every hidden leg are '
    'not required. Unconvincing essential construction means revise the image, '
    'not ask the producer to explain it. Preserve identity independently.'
)


def policy(cards=None):
    return {'version': 1, 'scope': 'cards' if cards is not None else 'all',
            'cards': list(cards or []), 'score_cannot_override': True}


def applies(state, key):
    p = state.get('construction_policy')
    return bool(p and (p['scope'] == 'all' or key in p['cards']))


def schema():
    from .schemas import obj, TEXT
    verdict = {'enum': ['pass', 'fail', 'uncertain']}
    check = obj({'verdict': verdict, 'evidence': TEXT})
    mechanism = obj({'applicability': {'enum': ['applicable', 'not_applicable']},
                     'closed_position_or_travel': TEXT, 'evidence': TEXT,
                     'verdict': verdict})
    assembly = obj({'object': TEXT, 'parts': {'type': 'array', 'items': TEXT, 'minItems': 1},
                    'part_whole': check, 'joints': check, 'mechanism': mechanism,
                    'support_and_contact': check, 'perspective': check,
                    'hand_or_actor_interaction': check,
                    'native_detail_region': TEXT, 'native_detail_inspected': {'const': True}})
    return obj({'artifact_path': TEXT, 'artifact_sha256': TEXT,
                'risk': {'enum': ['single_piece', 'assembled', 'interactive']},
                'assemblies': {'type': 'array', 'items': assembly, 'minItems': 1},
                'verdict': verdict})


def extend_schema(base, kind):
    import copy
    value = copy.deepcopy(base)
    if kind == 'card_review':
        value['properties']['construction_review'] = schema()
        value['required'].append('construction_review')
    elif kind == 'set_review':
        from .schemas import obj, TEXT
        value['properties']['construction_rechecks'] = {'type': 'array', 'items': obj({
            'card': TEXT, 'artifact_path': TEXT, 'artifact_sha256': TEXT,
            'verdict': {'enum': ['pass', 'fail', 'uncertain']}, 'evidence': TEXT})}
        value['required'].append('construction_rechecks')
    return value


def _nonblank(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Construction checks require concrete nonblank visible evidence.')


def validate_card(run, response, artifact, key):
    from .schemas import validate
    audit = response.get('construction_review')
    if audit is None:raise ValueError('Missing mandatory construction_review; numeric score cannot accept it.')
    validate(audit, schema())
    if (audit['artifact_path'] != artifact['path'] or audit['artifact_sha256'] != artifact['sha256']
            or sha(inside(run, artifact['path'])) != artifact['sha256']):
        raise ValueError('Construction review must bind the exact candidate pixels.')
    results = []
    if audit['risk'] == 'single_piece' and (len(audit['assemblies']) != 1 or any(
            len(a['parts']) != 1 or a['mechanism']['applicability'] == 'applicable' for a in audit['assemblies'])):
        raise ValueError('A multi-part or moving assembly cannot bypass final recheck as single_piece.')
    if audit['risk'] == 'assembled' and any(a['mechanism']['applicability'] == 'applicable' for a in audit['assemblies']):
        raise ValueError('A moving mechanism must declare interactive risk.')
    for assembly in audit['assemblies']:
        _nonblank(assembly['object']);_nonblank(assembly['native_detail_region'])
        for part in assembly['parts']:_nonblank(part)
        for name in ('part_whole', 'joints', 'mechanism', 'support_and_contact',
                     'perspective', 'hand_or_actor_interaction'):
            check = assembly[name]
            _nonblank(check['evidence']);results.append(check['verdict'])
        _nonblank(assembly['mechanism']['closed_position_or_travel'])
        if assembly['mechanism']['applicability'] == 'not_applicable' and assembly['mechanism']['verdict'] != 'pass':
            raise ValueError('Inapplicable mechanism needs an explained pass, not a hidden skipped failure.')
    expected = 'fail' if 'fail' in results else 'uncertain' if 'uncertain' in results else 'pass'
    if audit['verdict'] != expected:raise ValueError('Construction verdict contradicts its subchecks.')
    geometry = next((c['verdict'] for c in response['criteria'] if c['name'] == 'geometry'), None)
    if expected != 'pass':
        if geometry != 'fail' or response['verdict'] != 'fail' or response['action'] != 'revise':
            raise ValueError('Failed or unconvincing construction must fail geometry and revise, regardless of Q95.')
        if key not in response['affected_cards'] or not any(
                f['criterion'] == 'geometry' and f['severity'] == 'blocking' and key in f['targets']
                for f in response.get('findings', [])):
            raise ValueError('Construction failure needs a localized geometry blocker and exact affected card.')
    return expected == 'pass'


def targets(state):
    result = {}
    for key, artifact in state.get('accepted', {}).items():
        if not applies(state, key):continue
        history = next((r for r in reversed(state['art_history'].get(key, []))
                        if r['artifact']['job_id'] == artifact['job_id']), None)
        audit = (history or {}).get('construction_review')
        if not audit or audit['verdict'] != 'pass':
            raise ValueError('Accepted scoped card lacks construction acceptance: ' + key)
        if audit['risk'] != 'single_piece':result[key] = artifact
    return result


def validate_set(run, response, required):
    rows = response.get('construction_rechecks')
    if rows is None:raise ValueError('Missing final construction rechecks.')
    if len(rows) != len(required) or {r['card'] for r in rows} != set(required):
        raise ValueError('Final review must recheck every supplied risky construction exactly once.')
    for row in rows:
        artifact = required[row['card']]
        _nonblank(row['evidence'])
        if (row['artifact_path'] != artifact['path'] or row['artifact_sha256'] != artifact['sha256']
                or sha(inside(run, artifact['path'])) != artifact['sha256']):
            raise ValueError('Final construction recheck cites stale pixels.')
        if row['verdict'] != 'pass' and (response['verdict'] != 'fail' or response['action'] != 'revise'
                or row['card'] not in response['affected_cards'] or not any(
                    f['criterion'] == 'construction' and f['severity'] == 'blocking' and row['card'] in f['targets']
                    for f in response.get('findings', []))):
            raise ValueError('Unconvincing final construction must block the set and target a revision.')
