"""Deterministic gates supplement, but cannot replace, semantic/visual review."""

import hashlib
from PIL import Image


def pixel_digest(path):
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        return hashlib.sha256(str(rgba.size).encode() + rgba.tobytes()).hexdigest()


def identity(entity, variant_id):
    return entity["scope"], entity["id"], variant_id if entity["scope"] == "set" else None


def dependencies(plan, canon):
    cards = {f"{v['id']}/{c['id']}": (v, c) for v in plan["variants"] for c in v["cards"]}
    pinned = {identity(e, e.get("variant_id")) for e in canon}
    sources = {}
    for v in plan["variants"]:
        for e in v["entities"]:
            token = identity(e, v["id"])
            if token in pinned:
                continue
            source = e.get("source_card_id")
            if not source or source not in cards:
                raise ValueError("New entity needs an existing source_card_id: " + e["id"])
            sv, sc = cards[source]
            if e["id"] not in sc["entity_ids"] or (e["scope"] == "set" and sv["id"] != v["id"]):
                raise ValueError("Entity source must show that entity in its scope: " + e["id"])
            if token in sources and sources[token] != source:
                raise ValueError("Conflicting entity source cards: " + e["id"])
            sources[token] = source
    result = {key: set() for key in cards}
    for key, (v, c) in cards.items():
        for eid in c["entity_ids"]:
            source = sources.get(("set", eid, v["id"]), sources.get(("collection", eid, None)))
            if source and source != key:
                result[key].add(source)
    done = set()
    while len(done) < len(cards):
        ready = {k for k, deps in result.items() if k not in done and deps <= done}
        if not ready:
            raise ValueError("Cyclic canon dependencies; separate source appearances")
        done |= ready
    return result


def check_concepts(plan, canon):
    signatures = []
    for variant in plan["variants"]:
        cards = variant["cards"]
        subjects = [c["concept"]["canonical_subject"] for c in cards]
        if len(set(subjects)) != len(subjects):
            raise ValueError("Repeated canonical subject within a set")
        signature = frozenset(subjects)
        if signature in signatures:
            raise ValueError("Variants repeat the same ten subjects; a palette/title change is insufficient")
        signatures.append(signature)
        # Catch the actual compiler regression without inventing variety quotas.
        if all(len({c[field].strip().casefold() for c in cards}) == 1
               for field in ("composition", "materials")):
            raise ValueError("All ten cards share identical composition AND materials briefs")
    dependencies(plan, canon)


def check_findings(review, phase, card_keys):
    findings = review["findings"]
    criteria = {c["name"]: c["verdict"] for c in review["criteria"]}
    blocking = [f for f in findings if f["severity"] == "blocking"]
    if review["verdict"] == "pass" and blocking:
        raise ValueError("A passed review cannot contain blocking findings")
    for f in findings:
        if f["criterion"] not in criteria:
            raise ValueError("Finding cites unknown criterion")
        if f["severity"] == "blocking" and criteria[f["criterion"]] != "fail":
            raise ValueError("Blocking finding must fail its criterion")
        if not set(f["targets"]) <= (set(card_keys) | {"brief", "plan", "set"}):
            raise ValueError("Finding cites unknown target")
    for name, verdict in criteria.items():
        if verdict == "fail" and not any(f["criterion"] == name for f in blocking):
            raise ValueError("Every failed criterion needs an actionable blocking finding")
    if phase in {"card_review", "set_review"} and review["action"] == "revise":
        targets = {t for f in blocking for t in f["targets"] if t in card_keys}
        if not targets or not targets <= set(review["affected_cards"]):
            raise ValueError("Image revision must include the cards with blocking findings")
