"""Explicit producer-requested correction without erasing completed history."""
import copy
import shutil

from .io import inside, now, read, sha, write, event


def revise_card(pipeline, key, note):
    pipeline.check_snapshot()
    state, run = pipeline.state, pipeline.run
    if not pipeline.quality_v2:raise ValueError('Targeted construction amendment requires a v2 project; legacy v1 contracts remain unchanged.')
    if state['status'] != 'complete' or not note.strip():
        raise ValueError('Targeted revision needs a completed run and the producer correction note.')
    pipeline.find(key)
    from .workqueue import WorkQueue
    if 'orchestration' not in state:raise ValueError('Targeted correction currently requires the queued production protocol.')
    if any(j['status'] in ('ready', 'leased', 'submitted', 'uncertain') for j in WorkQueue(run).jobs()):
        raise ValueError('Resolve active work before starting a targeted revision.')
    if key not in state['accepted']:raise ValueError('Correction must identify a currently accepted card.')
    from .construction import policy
    from . import __version__
    number = len(state.get('producer_revisions', [])) + 1
    folder = inside(run, f'amendments/revision-{number:03d}')
    folder.mkdir(parents=True, exist_ok=False)
    previous = copy.deepcopy(state['accepted'][key])
    write(folder/'state-before.json', state)
    for name in ('report.html', 'review.html', 'audit.html', 'result-manifest.json',
                 'timing-data.json', 'delivery-receipt.json'):
        if (run/name).exists():shutil.copy2(run/name, folder/name)
    # Retire the old downloadable ZIP while the amended order is incomplete.
    if (run/'delivery.zip').exists():shutil.move(str(run/'delivery.zip'), str(folder/'delivery.zip'))
    if (run/'delivery-receipt.json').exists():(run/'delivery-receipt.json').unlink()
    p = state.get('construction_policy')
    if p is None:state['construction_policy'] = policy([key])
    elif p['scope'] == 'cards' and key not in p['cards']:p['cards'].append(key)
    write(folder/'request.json', {'time': now(), 'card': key, 'note': note,
          'previous_artifact': previous, 'executor_version': __version__,
          'construction_policy': state['construction_policy'],
          'other_cards_preserved': sorted(k for k in state['accepted'] if k != key),
          'history_and_attempt_counts_preserved': True})
    state.setdefault('producer_revisions', []).append({'path': (folder/'request.json').relative_to(run).as_posix(),
                                                      'sha256': sha(folder/'request.json')})
    state.setdefault('art_invalidated', []).append(previous['job_id'])
    state['accepted'].pop(key);state['art_stops'].pop(key, None)
    state['feedback']['image:'+key] = {'summary': note, 'source': 'explicit producer correction',
                                      'preserve': 'Other29 cards and the independently approved entity identity.'}
    state['orchestration'].get('card_blockers', {}).pop(key, None)
    state.update(status='running', phase='parallel_art', pending=None, current=None, reason='')
    event(run, 'producer_revision', card=key, note=note, previous_job=previous['job_id'],
          amendment=(folder/'request.json').relative_to(run).as_posix())
    pipeline.save()
    return {'card': key, 'status': 'revision_requested', 'other_cards_preserved': len(state['accepted']),
            'history': str(folder), 'attempts_reset': False}
