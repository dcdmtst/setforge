"""Shared, provider-independent art acceptance policy (no rounded decisions)."""

IDS = ['technical', 'palette', 'narrative', 'style', 'background', 'materials',
       'composition', 'lighting', 'interest']
WEIGHTS = [20, 10, 10, 15, 8, 12, 10, 5, 5]
THRESHOLD = 93.5
MAX_EDITS = 3
PLATEAU_DELTA = 0.5
PLATEAU_COUNT = 2


def policy(version=2):
    """Return a fresh snapshot; version 1 remains available for historical runs."""
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported art acceptance policy version')
    result = {'version': version, 'threshold': THRESHOLD,
            'max_edits': 5 if version == 1 else MAX_EDITS,
            'baseline_iteration': 0, 'plateau_delta': PLATEAU_DELTA,
            'plateau_nonimproving_edits': PLATEAU_COUNT,
            'weights': dict(zip(IDS, WEIGHTS)), 'normalizer': sum(WEIGHTS),
            'score_step': 5, 'compare_unrounded': True,
            'requires': ['full_inspected', 'thumbnail_inspected', 'class_pass',
                         'canon_pass', 'all_criteria_pass', 'no_blockers'],
            'stop_is_not_pass': True}
    if version == 2:
        result.update(plateau_action='change_strategy', exhaustion_action='discard')
    return result


def score(scores):
    if set(scores) != set(IDS):
        raise ValueError('Exactly nine common score dimensions required')
    if any(type(v) is not int or v < 0 or v > 100 or v % 5 for v in scores.values()):
        raise ValueError('Scores must be integers 0..100 in steps of five')
    return sum(scores[k] * w for k, w in zip(IDS, WEIGHTS)) / sum(WEIGHTS)


def _settings(acceptance_policy, threshold=None, max_edits=None, delta=None):
    settings = dict(policy() if acceptance_policy is None else acceptance_policy)
    version = settings.get('version', 1)
    defaults = policy(version)
    defaults.update(settings)
    for key, value in [('threshold', threshold), ('max_edits', max_edits),
                       ('plateau_delta', delta)]:
        if value is not None:
            defaults[key] = value
    if type(defaults['max_edits']) is not int or defaults['max_edits'] < 0:
        raise ValueError('max_edits must be a nonnegative integer')
    if (type(defaults['plateau_nonimproving_edits']) is not int
            or defaults['plateau_nonimproving_edits'] < 1):
        raise ValueError('plateau_nonimproving_edits must be a positive integer')
    return defaults


def plateau_signal(history, *, acceptance_policy=None, delta=None):
    """Report stagnation independently of whether this policy stops on it.

    Ineligible scores never improve the eligible best, even when numerically
    above the threshold. Callers can record the signal and change the next
    correction strategy without treating it as acceptance or exhaustion.
    """
    if not history:
        raise ValueError('At least one reviewed image is required')
    settings = _settings(acceptance_policy, delta=delta)
    best = history[0]['quality'] if history[0]['eligible'] else -1
    stagnant = 0
    for r in history[1:]:
        q = r['quality'] if r['eligible'] else -1
        stagnant = 0 if q >= best + settings['plateau_delta'] else stagnant + 1
        best = max(best, q)
    return stagnant >= settings['plateau_nonimproving_edits']


def decide(history, threshold=None, max_edits=None, delta=None, *, acceptance_policy=None):
    """Decide using the saved policy, or version 2 for a new/default run.

    An initial image is iteration zero. Version 2 allows three edits and
    discards a still-failing card; plateau is only a strategy-change signal.
    Explicit version-1 snapshots retain their original early plateau stop and
    five-edit limit. Passing numeric overrides alone does not select version 1.
    """
    if not history:
        raise ValueError('At least one reviewed image is required')
    settings = _settings(acceptance_policy, threshold, max_edits, delta)
    current = history[-1]
    if current['eligible'] and current['quality'] >= settings['threshold']:
        return 'accepted'
    if len(history) - 1 >= settings['max_edits']:
        return 'iteration_limit' if settings['version'] == 1 else 'discarded'
    if settings['version'] == 1 and plateau_signal(history, acceptance_policy=settings):
        return 'plateau'
    return 'revise'
