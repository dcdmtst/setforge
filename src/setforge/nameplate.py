"""Read-only, fixed-size English title preflight; no renaming or layout fitting.

An atlas renderer's natural pixel measurement is preferred. Explicit advances
are also supported, all measured at one baseline height and scaled uniformly
to the policy's fixed target height. Unknown glyphs get a conservative estimate
for diagnostics, but an estimate can never certify a title as fitting.
"""
import math
import re
from inspect import signature
from unicodedata import name as unicode_name


DEFAULT_POLICY = {'version': 1, 'target_height': 18, 'available_width': 160,
                  'set_target_height': 31, 'set_available_width': 850}
_FIT_FLAGS = ('fit_to_width', 'auto_shrink', 'font_shrunk', 'ellipsized',
              'cropped', 'widen_to_fit')


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be a finite positive number.')
    return value


def measure_text(text, metrics, *, target_height=18):
    """Measure explicit glyph advances without fitting to a width.

    metrics = {'advance': {'W': 14, 'I': 4, ' ': 6},
               'baseline_height': 18, 'tracking': 1,
               'unknown_advance': 18}.
    Tracking is applied only between characters. Unsupported glyphs use the
    supplied unknown_advance or the larger of baseline height / known advances.
    Such a result has complete=False and is not an acceptance measurement.
    """
    if not isinstance(text, str):
        raise ValueError('Title must be a string.')
    baseline = _positive(metrics['baseline_height'], 'baseline_height')
    target = _positive(target_height, 'target_height')
    advance = metrics['advance']
    if not isinstance(advance, dict) or not advance:
        raise ValueError('Explicit nonempty advance metrics are required.')
    for ch, value in advance.items():
        if not isinstance(ch, str) or len(ch) != 1:
            raise ValueError('Advance metrics must use single-character keys.')
        _positive(value, 'glyph advance')
    tracking = metrics.get('tracking', 0)
    if isinstance(tracking, bool) or not isinstance(tracking, (int, float)) or not math.isfinite(tracking) or tracking < 0:
        raise ValueError('tracking must be finite and nonnegative.')
    unknown_advance = _positive(metrics.get('unknown_advance', max(baseline, *advance.values())), 'unknown_advance')
    missing = sorted(set(text) - set(advance))
    width = (sum(advance.get(ch, unknown_advance) for ch in text)
             + max(0, len(text) - 1) * tracking) * target / baseline
    return {'text': text, 'width': width, 'ink_height': target,
            'baseline_height': baseline, 'complete': not missing,
            'unsupported_characters': missing, 'fit_to_width': False,
            'source': metrics.get('source', 'explicit glyph advance metrics')}


def preflight(plan, policy=None, measurefn=None):
    """Return per-label fit verdicts without mutating plan or writing files.

    measurefn(text, target_height) must return the natural width in pixels, or
    a dict with width and optional text/ink_height/fit flags. A one-argument
    callback is also supported, but must already use the fixed target height.
    Without a callback, policy['metrics'] supplies explicit advances.

    Card defaults: ink height 18, available width 160. Set headings default to
    ink height 31 and width 850; callers may explicitly set set_target_height
    and set_available_width for their distinct, fixed heading lane. No dimensions
    are changed in response to a particular title. producer_flagged_labels
    holds explicit label keys requiring review even if the width passes.
    English script is checked here; meaningful English remains editorial QA.
    """
    effective = {**DEFAULT_POLICY, **(policy or {})}
    if any(effective.get(flag) for flag in _FIT_FLAGS):
        raise ValueError('Title preflight forbids shrinking, ellipsis, cropping or widening to fit.')
    labels = []
    for variant in plan.get('variants', []):
        vid = variant['id']
        labels.append(('set:' + vid, 'set', variant.get('title')))
        labels.extend((vid + '/' + card['id'], 'card', card.get('title')) for card in variant['cards'])
    results = []
    flagged = set(effective.get('producer_flagged_labels', []))
    for key, kind, text in labels:
        heading = kind == 'set'
        height = _positive(effective.get('set_target_height', effective['target_height']) if heading else effective['target_height'], 'target_height')
        available = _positive(effective.get('set_available_width', effective['available_width']) if heading else effective['available_width'], 'available_width')
        item = {'key': key, 'kind': kind, 'text': text, 'target_height': height,
                'available_width': available, 'measured_width': None,
                'measurement_complete': False, 'measurement_source': None,
                'font_file_recovered': None, 'case_rescaled': [],
                'unsupported_characters': [], 'reasons': []}
        reasons = item['reasons']
        if not isinstance(text, str) or not text.strip():
            reasons.append('missing_title')
        else:
            if text != text.strip() or '\n' in text or '\r' in text or '\t' in text:
                reasons.append('title_must_be_one_unpadded_line')
            if not re.search('[A-Za-z]', text) or any(ch.isalpha() and not unicode_name(ch, '').startswith('LATIN ') for ch in text):
                reasons.append('english_source_title_required')
            callback = measurefn
            try:
                if callback is not None:
                    try:
                        signature(callback).bind(text, height)
                    except TypeError:
                        measured = callback(text)
                    except ValueError:
                        measured = callback(text, height)
                    else:
                        measured = callback(text, height)
                elif effective.get('metrics'):
                    measured = measure_text(text, effective['metrics'], target_height=height)
                else:
                    raise ValueError('No verified renderer or advance metrics supplied.')
                if isinstance(measured, (int, float)) and not isinstance(measured, bool):
                    measured = {'width': measured}
                if not isinstance(measured, dict):
                    raise ValueError('Measure function must return a width or measurement dict.')
                width = measured['width']
                if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or width <= 0:
                    raise ValueError('Natural pixel width must be finite and positive.')
                item['measured_width'] = width
                item['measurement_source'] = measured.get('source', 'caller-supplied natural pixel measurement')
                item['font_file_recovered'] = measured.get('font_file_recovered')
                item['case_rescaled'] = measured.get('case_rescaled', [])
                item['unsupported_characters'] = measured.get('unsupported_characters', [])
                item['measurement_complete'] = measured.get('complete', True) and not item['unsupported_characters']
                if not item['measurement_complete']:
                    reasons.append('unsupported_glyphs_not_certified')
                if any(measured.get(flag) for flag in _FIT_FLAGS):
                    reasons.append('forbidden_fit_transform')
                if measured.get('text', text) != text:
                    reasons.append('measured_text_differs_from_title')
                if measured.get('ink_height', height) != height:
                    reasons.append('wrong_fixed_text_height')
                if measured.get('available_width', available) != available:
                    reasons.append('wrong_fixed_nameplate_width')
                if width > available:
                    reasons.append('nameplate_overflow')
            except (KeyError, TypeError, ValueError, OSError) as exc:
                reasons.append('measurement_unavailable')
                item['measurement_error'] = str(exc)
        if key in flagged:
            reasons.append('producer_flagged_title_pending')
        item['verdict'] = 'fail' if reasons else 'pass'
        item['next_action'] = ('rewrite_title_then_replace_card_concept' if kind == 'card' else 'rewrite_set_title') if reasons else 'none'
        results.append(item)
    return {'version': 1, 'verdict': 'pass' if results and all(x['verdict'] == 'pass' for x in results) else 'fail',
            'read_only': True, 'fit_to_width': False, 'labels': results,
            'failed_labels': [x['key'] for x in results if x['verdict'] == 'fail']}
