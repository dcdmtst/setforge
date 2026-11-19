"""Provider-neutral worker preparation. These checks never accept a job or art.

Response preflight reads the exact saved request, not Pipeline's mutable current
pointer. Inspection cache records are explicit attestations AFTER actual viewing;
hashing/reading a file alone never marks it inspected. Keep this worker cache
outside immutable input snapshots. No producer wishes are changed here.
"""
import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .io import inside, now, read
from .quality import check_concepts, check_findings
from .schemas import validate


FRAMING_GUIDANCE = (
    "Keep the complete MAIN group or furniture ensemble inside the native image. "
    "Check the entire outer contour: widest skirt hem, elbows, ears, feet, chair "
    "wings/arms and table edges, not only faces. Leave a continuous strip of the "
    "ASSIGNED BACKGROUND around the widest contour, approximately 6–8% of canvas width at a risky "
    "side. If an edge is clipped, move the camera noticeably farther back or "
    "reduce/reposition the WHOLE group within the same native canvas; do not "
    "merely move a sleeve while the hem still hits the edge. Preserve the approved "
    "shared action, identities, part counts, attachments, contact/support and "
    "thumbnail readability. Natural table/body occlusion may remain; outer frame "
    "cropping of a focal body is different. A room strip applies only when a room "
    "is actually assigned (normally category 4/5). For floating category 1 keep "
    "abstract background and no support; do not introduce a room, floor or horizon. "
    "For categories 2/3 retain their assigned simple plane or material surface. "
    "The percentage is framing guidance, not an invented numeric gate. "
    "Do not trim the PNG, change report "
    "layout, stretch the canvas or alter producer wishes to conceal a defect."
)


def framing_guidance():
    """Reusable art direction, not a new mandatory numeric margin quota."""
    return FRAMING_GUIDANCE


def _card_keys(context):
    plan = context.get('plan', {})
    keys = {v['id'] + '/' + c['id'] for v in plan.get('variants', [])
            for c in v.get('cards', [])}
    variant, card = context.get('variant', {}), context.get('card', {})
    if variant.get('id') and card.get('id'):
        keys.add(variant['id'] + '/' + card['id'])
    keys.update(context.get('accepted_images', {}))
    return keys


def _review(request, response):
    kind, context = request['kind'], request.get('context', {})
    required = request.get('required_criteria')
    if kind == 'entity_review':
        required = ['identity', 'anatomy_geometry', 'materials', 'style', 'visibility']
    names = [x['name'] for x in response['criteria']]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate review criterion.')
    if required is not None and set(names) != set(required):
        raise ValueError('Review must cover exactly the requested criteria; missing or unknown criterion.')
    verdicts = {x['verdict'] for x in response['criteria']}
    expected = 'fail' if 'fail' in verdicts else 'uncertain' if 'uncertain' in verdicts else 'pass'
    if response['verdict'] != expected:
        raise ValueError('Overall verdict contradicts criterion verdicts.')
    if any(not x['evidence'].strip() for x in response['criteria']):
        raise ValueError('Every criterion needs nonblank evidence.')
    if kind == 'entity_review':
        entity, artifact = context['entity'], context['source_artifact']
        if (response['entity_id'] != entity['id']
                or response['source_card_id'] != entity['source_card_id']
                or response['artifact_path'] != artifact['path']):
            raise ValueError('Entity response must identify the exact requested entity/source artifact.')
        if expected == 'pass' and (not response['full_inspected'] or not response['thumbnail_inspected']
                                   or not response['approved_components']):
            raise ValueError('Entity pass requires full/thumbnail inspection and approved components.')
        return
    keys = _card_keys(context)
    affected = response['affected_cards']
    if len(affected) != len(set(affected)) or not set(affected) <= keys:
        raise ValueError('Affected cards must be distinct exact known variant/card keys, not variant IDs.')
    if expected == 'pass' and (response['action'] != 'none' or affected):
        raise ValueError('Passed review must have action=none and no affected cards.')
    if expected != 'pass' and response['action'] == 'none':
        raise ValueError('Failed/uncertain review needs an action.')
    if 'findings' in response:
        check_findings(response, kind, keys)
        for finding in response['findings']:
            if not finding['evidence'].strip() or not finding['fix'].strip():
                raise ValueError('Every finding needs nonblank evidence and a fix.')
    if kind == 'set_review' and expected == 'fail' and response['action'] == 'revise' and not affected:
        raise ValueError('Set revision must name exact known variant/card keys.')
    art = response.get('art_review')
    if art:
        if not art['full_inspected'] or not art['thumbnail_inspected']:
            raise ValueError('Scored review requires full native and thumbnail inspection.')
        if any(not x.strip() for x in art['evidence'].values()) or not art['class_evidence'].strip():
            raise ValueError('Scored review needs nonblank evidence for each score and class.')


def _plan_basic(context, plan):
    groups = plan['variants']
    expected = context.get('brief', {}).get('variants')
    if expected is not None and len(groups) != expected:
        raise ValueError('Variant count differs from the exact brief.')
    if len({v['id'] for v in groups}) != len(groups):
        raise ValueError('Duplicate variant ID.')
    catalog = context.get('reference_catalog')
    refs = {x['id'] for x in catalog} if catalog is not None else None
    collection = {e['id'] for e in context.get('collection_canon', []) if e['scope'] == 'collection'}
    for v in groups:
        cards = v['cards']
        if len({c['id'] for c in cards}) != len(cards):
            raise ValueError('Duplicate card ID within a set.')
        if len({c['subject'].strip().casefold() for c in cards}) != len(cards):
            raise ValueError('Identical card subjects within a set.')
        if not {1, 2, 3, 4} <= {c['background'] for c in cards}:
            raise ValueError('Each set needs all four background categories before art.')
        if context.get('production_contract') and not v.get('wishes_response', '').strip():
            raise ValueError('Every variant must respond to the original producer wishes.')
        declared = [e['id'] for e in v.get('entities', [])]
        if len(declared) != len(set(declared)):
            raise ValueError('Duplicate entity declaration within a set.')
        collection.update(e['id'] for e in v.get('entities', []) if e['scope'] == 'collection')
        known_entities = collection | set(declared)
        for c in cards:
            if refs is not None and not set(c['reference_ids']) <= refs:
                raise ValueError('Unknown reference ID in plan.')
            if not set(c['entity_ids']) <= known_entities or len(c['entity_ids']) != len(set(c['entity_ids'])):
                raise ValueError('Unknown or duplicate entity ID on card.')
    if groups and all('concept' in c for v in groups for c in v['cards']):
        check_concepts(plan, context.get('collection_canon', []))


def preflight_response(run, job_id, response):
    """Read-only transport/semantic preflight; return pass/fail diagnostics.

    ``pass`` means structurally ready to deliver, NEVER accepted by an art gate.
    The coordinator still validates and applies the result. Response may be a
    decoded dict or JSON path. No Pipeline instance, state write or accept call.
    Missing policies are explicitly skipped rather than silently invented.
    """
    result = {'job_id': job_id, 'verdict': 'pass', 'structural_preflight_only': True,
              'checks': [], 'errors': []}

    def check(name, fn):
        try:
            value = fn()
            result['checks'].append({'name': name, 'verdict': 'pass'})
            return value
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result['verdict'] = 'fail'
            result['checks'].append({'name': name, 'verdict': 'fail'})
            result['errors'].append({'check': name, 'message': str(exc)})

    def load_request():
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            raise ValueError('Invalid job ID.')
        request = read(inside(run, 'jobs/' + job_id + '/request.json'))
        if request.get('job_id', job_id) != job_id:
            raise ValueError('Saved request job ID differs from requested job.')
        return request

    request = check('request', load_request)
    if request is None:
        return result
    if isinstance(response, (str, Path)):
        response = check('response_json', lambda: read(response))
    check('output_schema', lambda: validate(response, request['output_schema']))
    if result['verdict'] == 'fail':
        return result
    kind, context = request['kind'], request.get('context', {})
    if kind.endswith('_review'):
        check('review_semantics', lambda: _review(request, response))
        if kind == 'card_review' and context.get('construction_policy'):
            from .construction import validate_card
            key = context['variant']['id'] + '/' + context['card']['id']
            check('construction', lambda: validate_card(run, response, context['construction_artifact'], key))
        if kind == 'set_review' and 'construction_targets' in context:
            from .construction import validate_set
            check('construction', lambda: validate_set(run, response, context['construction_targets']))
        if kind == 'entity_review':
            check('source_hash', lambda: _image_record(run, context['source_artifact']))
    if kind == 'plan':
        check('plan_structure', lambda: _plan_basic(context, response))
        for field in ('allocation_policy', 'nameplate_policy'):
            if not context.get(field):
                result['checks'].append({'name': field, 'verdict': 'skipped', 'reason': 'No saved policy in exact request.'})
        if context.get('allocation_policy'):
            from .allocation import check as allocation_check
            result['allocation'] = check('allocation_policy', lambda: allocation_check(response, context['allocation_policy']))
        if context.get('nameplate_policy'):
            from .nameplate import preflight
            from .typography import for_run
            def names():
                lettering=for_run(run,frozen=True)
                report = preflight(response, context['nameplate_policy'], lettering.measure_label)
                report['typography']=lettering.public_profile()
                result['nameplate'] = report
                if report['verdict'] != 'pass':
                    raise ValueError('English nameplate fit failed. Rewrite the title, then replace concept if needed; never shrink, crop, ellipsize or widen to fit.')
            check('nameplate_policy', names)
    return result


def _image_record(run, image):
    path = inside(run, image['path'])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if image.get('sha256') is not None and image['sha256'] != digest:
        raise ValueError('Image content hash differs from the supplied inspected/request hash: ' + image['path'])
    return {**image, 'sha256': digest}


def _cacheable(role):
    return bool(re.fullmatch(r'original_style_reference:[^\s:]+|canon:[^\s:]+:v[0-9]+', role))


class InspectedReferenceCache:
    """Persistent attestations keyed by worker identity + byte hash + exact role.

    Share a cache path between runs if desired. Changing worker, canon version,
    role or file bytes requires another view. Candidates (including identical
    rejected pixels), thumbnails, edit targets and entity-source candidates are
    deliberately never cacheable. Entries do not certify any visual criterion.
    """
    def __init__(self, cache_path, worker):
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError('A nonblank worker identity is required.')
        self.path, self.worker = Path(cache_path), worker

    def uninspected(self, run, images):
        """Return uninspected records with current hashes; never mark by reading."""
        records = [_image_record(run, item) for item in images]
        known = set()
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                known = {(row[0], row[1]) for row in db.execute(
                    'SELECT sha256,role FROM inspections WHERE worker=?', (self.worker,))}
        return [x for x in records if not _cacheable(x.get('role', ''))
                or (x['sha256'], x['role']) not in known]

    def mark_inspected(self, run, image, *, expected_sha256, evidence):
        """Explicit AFTER-VIEW attestation; reject changed content and unsafe roles."""
        if not _cacheable(image.get('role', '')):
            raise ValueError('Only original style references and qualified canon roles are cacheable; candidates/thumbnails/edit targets require a fresh view.')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('Explicit visual inspection evidence is required.')
        if not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256):
            raise ValueError('The SHA-256 recorded before visual inspection is required.')
        record = _image_record(run, image)
        if record['sha256'] != expected_sha256:
            raise ValueError('Image changed since visual inspection; view the current bytes again.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS inspections (worker TEXT,sha256 TEXT,role TEXT,path TEXT,inspected_at TEXT,evidence TEXT,PRIMARY KEY(worker,sha256,role))')
                db.execute('INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?)',
                           (self.worker, record['sha256'], record['role'], record['path'], now(), evidence.strip()))
        return record
