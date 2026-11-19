"""Explicit cast staging for new runs; semantic and visual review remain mandatory.

These tags are planning assertions, not measurements of the rendered pixels.  A
different tag cannot excuse the same visible pose or camera in two illustrations.
Historical runs opt out by not calling this gate / not extending their schema.
"""

import copy
from itertools import combinations


STANCES = (
    'standing', 'seated', 'kneeling', 'crouching', 'bending', 'leaning',
    'walking', 'running', 'jumping', 'balancing', 'lying', 'hanging',
)
GESTURES = (
    'arms_resting', 'arms_raised', 'arms_spread', 'one_hand_reaching',
    'two_hands_working', 'carrying', 'pointing', 'clapping', 'waving',
    'hands_together', 'hands_on_hips', 'supporting_body',
)
AZIMUTHS = (
    'front', 'left_three_quarter', 'right_three_quarter', 'left_profile',
    'right_profile', 'rear_left_three_quarter', 'rear_right_three_quarter', 'rear',
)
ELEVATIONS = ('eye_level', 'high', 'low', 'overhead')


def policy():
    return {
        'version': 1,
        'same_character_requires': ['different_pose', 'different_camera'],
        'scope': 'all appearances within a set; collection identities across sets',
        'adjacency_review': True,
        'visual_review_required': True,
    }


def staging_schema():
    """Card field schema. One entry per visible key character, zero for props."""
    text = {'type': 'string', 'minLength': 1}
    fields = {
        'character_id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,47}$'},
        'stance': {'enum': list(STANCES)},
        'gesture': {'enum': list(GESTURES)},
        'action': text,
        'camera_azimuth': {'enum': list(AZIMUTHS)},
        'camera_elevation': {'enum': list(ELEVATIONS)},
    }
    return {'type': 'array', 'items': {
        'type': 'object', 'properties': copy.deepcopy(fields),
        'required': list(fields), 'additionalProperties': False,
    }}


PLANNING_GUIDANCE = (
    'For each card fill character_staging: one entry per visible key character, '
    'or [] for a card without key characters. Use the recurring entity ID when '
    'that character belongs to canon; otherwise use a stable set-scoped character '
    'ID, not a fresh alias for the same hero. Repeated appearances of one hero '
    'must change BOTH the bodily pose (stance or gesture) AND the camera '
    '(azimuth or elevation). Describe the concrete visible action in action and '
    'in the scene brief. Tiny arm, head or camera adjustments and mirror copies '
    'are insufficient. High/low views must visibly change the view of the body, '
    'not only the horizon. Within each 5x2 set, inspect side-by-side character '
    'cards especially carefully. Different characters may replace repeated heroes, '
    'but should be visually distinct, not the same model with a new name. '
    'This is an art-direction requirement, not permission to change canon identity.'
)


REVIEW_GUIDANCE = (
    'Independently compare every repeated character appearance and neighboring '
    'character cards in the final 5x2 layout. Inspect native PNGs AND thumbnails. '
    'The same hero must have a materially different bodily pose AND camera angle; '
    'different props, titles, expression, mirrored copies or staging tags do not '
    'prove this. Judge the visible silhouette, body action, perspective, face and '
    'costume. Different named heroes that visibly look like the same character '
    'in the same pose also need revision. A visually repeated pose OR repeated '
    'camera for the same hero is a blocking visual-diversity failure, regardless '
    'of a passing numerical art score. Tags only identify pairs to inspect. '
    'Before scores, verify every explicit framing constraint against the native '
    'PNG: no clipped ears, feet, hands, tools or main silhouette, and the required '
    'clear margins must actually be visible. Any unmet mandatory framing '
    'requirement blocks acceptance; a high aggregate score cannot override it. '
    'Audit physical construction and contact in the actual pixels: connected '
    'parts, joints, support, lid/drawer fit and travel, hand/prop contact. '
    'An implausible essential assembly or unresolved uncertainty blocks acceptance.'
)


def _scope(variant, character_id, collection_ids):
    entity = next((e for e in variant.get('entities', [])
                   if e['id'] == character_id), None)
    if entity and entity['scope'] == 'set':
        return variant['id'], character_id
    return (None if character_id in collection_ids else variant['id'], character_id)


def check_staging(plan, canon=()):
    """Validate planning tags for an explicitly opted-in, new-run plan.

    Call only when the immutable run carries character_staging_policy.  This
    intentionally does not infer missing tags for old plans or judge images.
    """
    # Keep standalone callers as strict as the full production-plan validator.
    from .schemas import validate
    schema = staging_schema()
    collection_ids = {e['id'] for v in plan['variants'] for e in v.get('entities', [])
                      if e['scope'] == 'collection'}
    collection_ids.update(e['id'] for e in canon if e['scope'] == 'collection')
    appearances = {}
    for variant in plan['variants']:
        for card in variant['cards']:
            key = variant['id'] + '/' + card['id']
            if 'character_staging' not in card:
                raise ValueError('Missing character_staging: ' + key)
            entries = card['character_staging']
            validate(entries, schema)
            if len(entries) != card['key_character_count']:
                raise ValueError('character_staging count must match key_character_count: ' + key)
            ids = [s['character_id'] for s in entries]
            if len(set(ids)) != len(ids):
                raise ValueError('Duplicate character identity within a card: ' + key)
            for staging in entries:
                if not staging['action'].strip():
                    raise ValueError('Character action must describe visible evidence: ' + key)
                token = _scope(variant, staging['character_id'], collection_ids)
                appearances.setdefault(token, []).append((key, staging))
    for (_, character_id), cards in appearances.items():
        for (left, a), (right, b) in combinations(cards, 2):
            same_pose = (a['stance'], a['gesture']) == (b['stance'], b['gesture'])
            same_camera = (a['camera_azimuth'], a['camera_elevation']) == (
                b['camera_azimuth'], b['camera_elevation'])
            if same_pose or same_camera:
                missing = ' and '.join(name for name, same in (
                    ('pose', same_pose), ('camera angle', same_camera)) if same)
                raise ValueError(
                    f'Repeated character {character_id} needs different {missing}: '
                    f'{left}, {right}. Different actions/titles alone are insufficient.')
    return True
