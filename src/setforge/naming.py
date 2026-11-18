"""Display copy is versioned separately from immutable production history."""
import json
from pathlib import Path

def names(run):
    p = Path(run) / 'display-titles.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}

def display_title(mapping, variant, card=None):
    item = mapping.get('sets', {}).get(variant['id'], {})
    return (item.get('cards', {}).get(card['id'], card['title']) if card
            else item.get('title', variant['title']))
