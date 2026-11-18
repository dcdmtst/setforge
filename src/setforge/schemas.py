"""Public JSON contracts shared by agents, APIs and command workers."""

import copy
from jsonschema import Draft202012Validator
from .acceptance import IDS


def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
TEXTS = {"type": "array", "items": TEXT}
IDENT = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,47}$"}

BRIEF = obj({
    "theme": TEXT, "wishes": {"type": "string"}, "required": TEXTS,
    "forbidden": TEXTS, "variants": {"type": "integer", "minimum": 3, "maximum": 5},
})
BRIEF["required"] = ["theme"]
BRIEF['properties'].update({
    'star_counts': {'type': 'array', 'minItems': 5, 'maxItems': 5,
                    'items': {'type': 'integer', 'minimum': 0, 'maximum': 10}},
    'characters': {'enum': ['auto', 'none', 'optional', 'required']},
    'production_mode': {'enum': ['full', 'pilot']},
})

ENTITY = obj({
    "id": IDENT, "scope": {"enum": ["set", "collection"]},
    "description": TEXT, "invariants": {"type": "array", "items": TEXT, "minItems": 1},
    "allowed_changes": TEXTS,
})

CARD = obj({
    "id": IDENT, "title": TEXT, "subject": TEXT, "semantic_role": TEXT,
    "background": {"type": "integer", "minimum": 1, "maximum": 4},
    "scene": TEXT, "composition": TEXT, "lighting": TEXT,
    "palette": TEXT, "materials": TEXT,
    "reference_ids": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 4},
    "entity_ids": TEXTS, "preserve": TEXTS,
    "main_cast_scene": {"type": "boolean"},
})

VARIANT = obj({
    "id": IDENT, "title": TEXT, "premise": TEXT, "difference": TEXT,
    "entities": {"type": "array", "items": ENTITY},
    "cards": {"type": "array", "items": CARD, "minItems": 10, "maxItems": 10},
})

PLAN = obj({"variants": {"type": "array", "items": VARIANT, "minItems": 3, "maxItems": 5}})

CRITERION = obj({"name": TEXT, "verdict": {"enum": ["pass", "fail", "uncertain"]},
                 "evidence": TEXT})
REVIEW = obj({
    "verdict": {"enum": ["pass", "fail", "uncertain"]},
    "summary": TEXT,
    "criteria": {"type": "array", "items": CRITERION, "minItems": 1},
    "action": {"enum": ["none", "revise", "replan", "clarify"]},
    "affected_cards": TEXTS,
})

IMAGE = obj({"image_path": TEXT, "note": {"type": "string"}})

ENTITY_REVIEW = obj({
    'entity_id': IDENT, 'source_card_id': TEXT, 'artifact_path': TEXT,
    'verdict': {'enum': ['pass', 'fail', 'uncertain']},
    'full_inspected': {'type': 'boolean'}, 'thumbnail_inspected': {'type': 'boolean'},
    'criteria': {'type': 'array', 'items': CRITERION, 'minItems': 5, 'maxItems': 5},
    'approved_components': TEXTS, 'excluded_components': TEXTS, 'feedback': TEXT,
})

SCHEMAS = {"brief": BRIEF, "plan": PLAN, "review": REVIEW, "image": IMAGE,
           'entity-review': ENTITY_REVIEW}

# Old immutable runs keep their original contracts. New projects opt into v2.
CONCEPT = obj({"canonical_subject": IDENT, "family": IDENT, "role": TEXT,
               "hook": TEXT, "visible_evidence": TEXT, "silhouette": TEXT,
               "genre_anchor": {"type": "boolean"}})
PLAN_V2 = copy.deepcopy(PLAN)
_v = PLAN_V2["properties"]["variants"]["items"]
_c = _v["properties"]["cards"]["items"]
_c["properties"]["concept"] = CONCEPT
_c["required"].append("concept")
_e = _v["properties"]["entities"]["items"]
_e["properties"]["source_card_id"] = {"type": ["string", "null"]}
_e["required"].append("source_card_id")
FINDING = obj({"code": IDENT, "criterion": TEXT,
               "severity": {"enum": ["blocking", "minor"]},
               "targets": {"type": "array", "items": TEXT, "minItems": 1},
               "evidence": TEXT, "fix": TEXT})
REVIEW_V2 = copy.deepcopy(REVIEW)
REVIEW_V2["properties"]["findings"] = {"type": "array", "items": FINDING}
REVIEW_V2["required"].append("findings")

ART_REVIEW = obj({
    'scores': obj({k: {'type': 'integer', 'minimum': 0, 'maximum': 100,
                       'multipleOf': 5} for k in IDS}),
    'evidence': obj({k: TEXT for k in IDS}),
    'class_pass': {'type': 'boolean'}, 'class_evidence': TEXT,
    'canon_pass': {'type': 'boolean'},
    'full_inspected': {'type': 'boolean'}, 'thumbnail_inspected': {'type': 'boolean'},
    'feedback': {'type': 'string'}, 'next_strategy': {'type': 'string'},
})


def scored_card_review(version=2, allocated=False):
    schema = copy.deepcopy(REVIEW_V2 if version == 2 else REVIEW)
    schema['properties']['art_review'] = ART_REVIEW
    schema['required'].append('art_review')
    if allocated:
        art = schema['properties']['art_review'] = copy.deepcopy(ART_REVIEW)
        art['properties'].update(observed_category={'type': 'integer', 'minimum': 1, 'maximum': 5},
                                 category_evidence=TEXT)
        art['required'] += ['observed_category', 'category_evidence']
    return schema


SCHEMAS.update({'art-review': ART_REVIEW, 'card-review': scored_card_review()})


def contracts(version):
    if version not in (1, 2):
        raise ValueError("Unsupported project schema_version")
    return (PLAN_V2, REVIEW_V2) if version == 2 else (PLAN, REVIEW)


def production_plan(version=2, wishes_response=True):
    schema = copy.deepcopy(contracts(version)[0])
    variant = schema['properties']['variants']['items']
    variant['properties']['cast_reason'] = TEXT
    variant['required'].append('cast_reason')
    if wishes_response:
        variant['properties']['wishes_response'] = TEXT
        variant['required'].append('wishes_response')
    card = variant['properties']['cards']['items']
    card['properties']['background']['maximum'] = 5
    extras = {'stars': {'type': 'integer', 'minimum': 1, 'maximum': 5},
              'golden': {'type': 'boolean'}, 'rarity_reason': TEXT,
              'category_reason': TEXT,
              'key_character_count': {'type': 'integer', 'minimum': 0}}
    card['properties'].update(extras)
    card['required'] += list(extras)
    return schema


SCHEMAS.update({'production-plan': production_plan(), 'production-card-review': scored_card_review(allocated=True)})


def validate(data, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        raise ValueError("; ".join(f"{'.'.join(map(str, e.path)) or '$'}: {e.message}"
                                   for e in errors[:12]))
