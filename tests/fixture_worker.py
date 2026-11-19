"""OFFLINE TEST FIXTURES ONLY. Never use this worker for production art or QA."""

import argparse
import json
import sys
from pathlib import Path


def plan(version=1, allocated=True, variants_count=3):
    variants = []
    subjects = ["shuttle", "helmet", "rover", "radio", "telescope", "beacon", "sample", "module", "map", "battery"]
    for v in range(variants_count):
        variants.append({
            "id": f"v{v+1}", "title": "TEST FIXTURE "+['ONE','TWO','THREE','FOUR','FIVE'][v], "premise": "Infrastructure test only",
            "difference": f"Test interpretation {v+1}",
            "entities": [{"id": "test-rover", "scope": "collection", "description": "Test rover",
                          "invariants": ["Orange body"], "allowed_changes": ["Environment"]}],
            "cards": [{"id": f"c{n+1}", "title": name, "subject": name,
                       "semantic_role": f"test role {n}", "background": n % 4 + 1,
                       "scene": "Test scene", "composition": "Dominant object", "lighting": "Soft",
                       "palette": "Orange and teal", "materials": "Matte metal",
                       "reference_ids": ["ref-001"], "entity_ids": ["test-rover"] if n == 2 else [],
                       "preserve": ["Object silhouette"], "main_cast_scene": False}
                      for n, name in enumerate(subjects)]})
    if version == 2:
        for v in variants:
            v["entities"][0]["source_card_id"] = "v1/c3"
            for c in v["cards"]:
                c["concept"] = {"canonical_subject": v["id"] + "-" + c["subject"],
                                "family": "test", "role": c["semantic_role"],
                                "hook": "TEST hook", "visible_evidence": "TEST evidence",
                                "silhouette": "TEST silhouette", "genre_anchor": True}
                c["composition"] += " " + c["subject"]
                c["materials"] += " " + c["subject"]
    if allocated:
        for v in variants:
            v['cast_reason'] = 'TEST deliberate object-only set; cast is not required.'
            v['wishes_response'] = 'TEST no extra wishes; only fixture transport and gate behavior are tested.'
            for n,c in enumerate(v['cards']):
                c.update(stars=n//2+1, golden=False, key_character_count=0,
                         rarity_reason='TEST ordinal allocation, not art complexity.',
                         category_reason='TEST category matches the test scene.')
    return {"variants": variants}


def review(request, verdict="pass", action="none", affected=None):
    names = {
        "brief_review": ["clarity", "conflicts"],
        "plan_review": ["theme", "required_constraints", "backgrounds", "semantic_diversity", "variant_differences", "canon"],
        "card_review": ["brief_match", "style", "composition", "background", "materials", "canon", "no_game_ui"],
        "set_review": ["coherence", "style_consistency", "semantic_diversity", "visual_diversity", "variant_differences", "canon", "required_constraints"],
    }[request["kind"]]
    names = request.get("required_criteria", names)
    result = {"verdict": verdict, "summary": "TEST FIXTURE judgement; no real art validation",
            "criteria": [{"name": name, "verdict": verdict, "evidence": "TEST FIXTURE only"} for name in names],
            "action": action, "affected_cards": affected or []}
    if "findings" in request["output_schema"]["properties"]:
        result["findings"] = []
    if 'art_review' in request['output_schema']['properties']:
        from setforge.acceptance import IDS
        result['art_review'] = {
            'scores': dict.fromkeys(IDS, 95),
            'evidence': dict.fromkeys(IDS, 'TEST FIXTURE only; no actual art judgement'),
            'class_pass': True, 'class_evidence': 'TEST FIXTURE not applicable',
            'canon_pass': True, 'full_inspected': True, 'thumbnail_inspected': True,
            'feedback': 'TEST FIXTURE fix' if verdict != 'pass' else '',
            'next_strategy': 'TEST FIXTURE strategy' if verdict != 'pass' else '',
        }
        if 'observed_category' in request['output_schema']['properties']['art_review']['properties']:
            result['art_review'].update(observed_category=request['context']['card']['background'],
                                       category_evidence='TEST FIXTURE category only')
    if 'construction_review' in request['output_schema']['properties']:
        artifact = request['context']['construction_artifact']
        check = lambda: {'verdict': 'pass', 'evidence': 'TEST FIXTURE structure only; no visual judgement'}
        result['construction_review'] = {
            'artifact_path': artifact['path'], 'artifact_sha256': artifact['sha256'],
            'risk': 'single_piece', 'verdict': 'pass',
            'assemblies': [{'object': 'TEST FIXTURE', 'parts': ['TEST FIXTURE'],
                'part_whole': check(), 'joints': check(), 'support_and_contact': check(),
                'perspective': check(), 'hand_or_actor_interaction': check(),
                'mechanism': {**check(), 'applicability': 'not_applicable',
                              'closed_position_or_travel': 'TEST FIXTURE has no moving parts'},
                'native_detail_region': 'TEST FIXTURE entire canvas', 'native_detail_inspected': True}]}
    if 'construction_rechecks' in request['output_schema']['properties']:
        result['construction_rechecks'] = [{'card': key, 'artifact_path': a['path'],
            'artifact_sha256': a['sha256'], 'verdict': 'pass', 'evidence': 'TEST FIXTURE only'}
            for key, a in request['context']['construction_targets'].items()]
    return result


def respond(request, run):
    if request['kind'] == 'entity_review':
        # Explicitly labelled transport fixture; it does not inspect pixels or
        # substitute for an art director in any production invocation.
        entity = request['context']['entity']
        return {'entity_id': entity['id'], 'source_card_id': entity['source_card_id'],
                'artifact_path': request['context']['source_artifact']['path'],
                'verdict': 'pass', 'full_inspected': True, 'thumbnail_inspected': True,
                'criteria': [{'name': name, 'verdict': 'pass',
                              'evidence': 'OFFLINE TEST FIXTURE ONLY, not actual visual QA'}
                             for name in ('identity', 'anatomy_geometry', 'materials', 'style', 'visibility')],
                'approved_components': ['OFFLINE FIXTURE declared identity transport only'],
                'excluded_components': ['Whole-card quality is not established by this entity fixture'],
                'feedback': 'OFFLINE TEST FIXTURE ONLY; independent identity transport passed'}
    if request["kind"] == "plan":
        card_schema = request["output_schema"]["properties"]["variants"]["items"]["properties"]["cards"]["items"]
        result=plan(2 if "concept" in card_schema["properties"] else 1, allocated='stars' in card_schema['properties'],variants_count=request['context']['brief'].get('variants',3))
        if 'wishes_response' not in request['output_schema']['properties']['variants']['items']['properties']:
            for v in result['variants']:v.pop('wishes_response',None)
        if request['context'].get('allocation_policy',{}).get('characters')=='required':
            for v in result['variants']:
                v['cast_reason']='TEST required two-character scene.'
                no_gold=request['context']['allocation_policy'].get('exclude_golden_cards',False)
                v['cards'][-1].update(background=4 if no_gold else 5,golden=not no_gold,main_cast_scene=True,key_character_count=2,
                                      scene='TEST two key characters in a small emotional scene.')
        if request['context'].get('character_staging_policy'):
            for v in result['variants']:
                for c in v['cards']:
                    c['character_staging']=[{'character_id': 'fixture-person-'+str(i), 'stance':'standing',
                        'gesture':'arms_resting','action':'OFFLINE fixture only', 'camera_azimuth':'front',
                        'camera_elevation':'eye_level'} for i in range(c.get('key_character_count',0))]
        return result
    if request['kind']=='replacement_plan':
        import copy
        c=copy.deepcopy(request['context']['card']); n=len(request['context']['rejected_candidates'])+1
        c['subject']='OFFLINE different replacement '+str(n)
        c['scene']='OFFLINE replacement scene '+str(n)
        c['title']='New '+str(n)
        c['concept']['canonical_subject']='replacement-'+str(n)
        return c
    if request['kind']=='replacement_review':
        import copy
        r=copy.deepcopy(request);r['kind']='plan_review'
        return review(r)
    if request["kind"] == "image":
        from PIL import Image, ImageDraw
        # Explicit labelled test pattern, never presented as generated artwork.
        number = int(request["job_id"].split("-")[0])
        image = Image.new("RGB", (512, 576), (number % 255, 80, 130))
        ImageDraw.Draw(image).text((24, 24), "TEST FIXTURE - NOT ART\n" + request["job_id"], fill="white")
        target = Path(run) / request["suggested_output_path"]
        image.save(target)
        return {"image_path": str(target), "note": "Labelled offline infrastructure test pattern"}
    return review(request)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    request_path = Path(args.request)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    data = respond(request, Path.cwd())
    Path(args.response).write_text(json.dumps(data), encoding="utf-8")
