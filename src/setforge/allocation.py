"""Mandatory pre-art allocation of stars, illustration categories and cast policy."""
import hashlib
import json


def policy(config, brief):
    counts = brief.get('star_counts', [2, 2, 2, 2, 2])
    if (len(counts) != 5 or any(type(x) is not int or x < 0 for x in counts)
            or sum(counts) != 10):
        raise ValueError('star_counts must contain five nonnegative integers summing to ten.')
    mode = brief.get('characters', config['rules'].get('characters', 'optional'))
    if mode == 'auto':
        mode = 'optional'
    if config['rules'].get('exclude_main_cast_scenes', False):
        if mode == 'required':
            raise ValueError('Required characters conflict with the project exclusion; resolve the brief.')
        mode = 'none'
    if mode not in ('none', 'optional', 'required'):
        raise ValueError('characters must be none, optional or required.')
    return {'version': 1, 'star_counts': counts, 'star_counts_source':
            'producer' if 'star_counts' in brief else 'working_default_not_reference_rule',
            'stars_mean': 'assigned ordinal rarity labels; drop rates are unspecified',
            'required_categories': [1, 2, 3, 4], 'characters': mode,
            'characters_source': ('project_exclusion' if config['rules'].get('exclude_main_cast_scenes', False)
                                  else 'producer' if 'characters' in brief else 'project_or_optional_default'),
            'golden_category': 5, 'golden_key_characters': [1, 2],
            'exclude_golden_cards': config['rules'].get('exclude_golden_cards', False),
            'rarity_changes_object_art': False}


def fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check(plan, allocation_policy):
    """Return an auditable matrix. Category/rarity linkage is deliberately not imposed."""
    groups = []
    for variant in plan['variants']:
        cards = variant['cards']
        if len(cards) != 10 or len({c['id'] for c in cards}) != 10:
            raise ValueError('Allocation requires ten unique cards per variant.')
        if not variant.get('cast_reason', '').strip():
            raise ValueError('Every variant must explain its deliberate cast decision.')
        categories, counts, cast = set(), [0] * 5, 0
        rows = []
        for c in cards:
            if type(c.get('stars')) is not int or not 1 <= c['stars'] <= 5:
                raise ValueError('Every card needs explicit stars 1..5 before art.')
            if type(c.get('golden')) is not bool:
                raise ValueError('Every card needs an explicit golden flag, independent of stars.')
            category = c.get('background')
            if type(category) is not int or category not in range(1, 6):
                raise ValueError('Every card needs an illustration category 1..5 before art.')
            for field in ('category_reason', 'rarity_reason'):
                if not c.get(field, '').strip():
                    raise ValueError('Category and rarity assignments need concrete reasons.')
            n = c.get('key_character_count')
            if type(n) is not int or n < 0:
                raise ValueError('Declare a nonnegative key-character count.')
            # Gold implies a cast scene; a cast scene does NOT imply gold.
            if c['golden'] and (category != 5 or n not in (1, 2)):
                raise ValueError('Golden cards require category 5 and one or two key characters; stars do not imply gold.')
            if bool(c['main_cast_scene']) != (n > 0):
                raise ValueError('Character count must agree with main_cast_scene.')
            if category == 5 and not c['golden']:
                raise ValueError('Category 5 is reserved for golden scenes; ordinary cast scenes may use category 4.')
            if c['golden'] and allocation_policy.get('exclude_golden_cards', False):
                raise ValueError('Golden cards are excluded; this does not prohibit ordinary character scenes.')
            if allocation_policy['characters'] == 'none' and n:
                raise ValueError('Character scenes are excluded by this explicit project/brief policy.')
            counts[c['stars'] - 1] += 1
            categories.add(category)
            cast += bool(n)
            rows.append({k: c[k] for k in ('id', 'title', 'stars', 'golden', 'background',
                                          'key_character_count', 'category_reason', 'rarity_reason')})
        if not set(allocation_policy['required_categories']) <= categories:
            raise ValueError('Allocation must contain all four categories 1,2,3,4 in every set, regardless of stars.')
        if counts != allocation_policy['star_counts']:
            raise ValueError('Star distribution differs from the saved allocation policy.')
        if allocation_policy['characters'] == 'required' and not cast:
            raise ValueError('The brief requires characters, but the variant contains no character scene.')
        groups.append({'id': variant['id'], 'title': variant['title'], 'star_counts': counts,
                       'category_counts': {str(k): sum(c['background'] == k for c in cards) for k in sorted(categories)},
                       'character_scenes': cast, 'cast_reason': variant['cast_reason'], 'cards': rows})
    if not groups:
        raise ValueError('Allocation has no variants.')
    return {'verdict': 'pass', 'plan_sha256': fingerprint(plan), 'policy': allocation_policy, 'variants': groups}


def require_gate(state):
    if 'allocation_policy' not in state:
        return  # Historical runs keep their contract.
    gate = state.get('allocation_gate')
    if not gate or gate['verdict'] != 'pass' or gate['plan_sha256'] != fingerprint(state['plan']):
        raise ValueError('Pre-art allocation gate missing or stale; no image generation/export allowed.')
    check(state['plan'], state['allocation_policy'])
