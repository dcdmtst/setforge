"""Read-only reconstruction of recorded work; no inferred model call timestamps."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STAGES = {
    'brief_review': 'Проверка брифа', 'plan': 'Составление наборов',
    'plan_review': 'Приёмка составов', 'image_initial': 'Иллюстрации: первые версии',
    'image_edit': 'Иллюстрации: переделки', 'card_review': 'Приёмка карточек',
    'entity_review': 'Приёмка канона', 'set_review': 'Приёмка наборов',
}
COLORS = {'brief_review': '#a78bfa', 'plan': '#60a5fa', 'plan_review': '#c084fc',
          'image_initial': '#34d399', 'image_edit': '#fbbf24', 'card_review': '#f472b6',
          'entity_review': '#22d3ee', 'set_review': '#fb923c'}


def _read(path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _local(value):
    if value is None:
        return '—'
    utc = datetime.fromtimestamp(value, timezone.utc)
    try:
        local = utc.astimezone(ZoneInfo('Europe/Madrid'))
    except ZoneInfoNotFoundError:
        # Windows installations need not ship the IANA database.  EU daylight
        # saving switches at 01:00 UTC on the last Sundays of March/October.
        def switch(month):
            last = datetime(utc.year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
            return (last - timedelta(days=(last.weekday() + 1) % 7)).replace(hour=1)
        hours = 2 if switch(3) <= utc < switch(10) else 1
        local = utc.astimezone(timezone(timedelta(hours=hours)))
    return local.isoformat(timespec='seconds')


def _duration(seconds):
    if seconds is None:
        return '—'
    seconds = round(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f'{hours} ч ' if hours else '') + (f'{minutes} мин ' if minutes or hours else '') + f'{seconds} с'


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def collect_timing(run, state):
    """Return auditable JSON data without modifying queue, state or artifacts.

    The caller may persist the returned value as ``timing-data.json``.  Model
    seconds are separately recorded tool durations, never positioned on a
    timeline by subtracting them from a later copy/delivery timestamp.
    """
    run = Path(run).resolve()
    jobs, events = [], []
    path = run / 'queue.sqlite3'
    if path.is_file():
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            # One consistent read snapshot, including when producers are live.
            db.execute('BEGIN')
            jobs = [dict(row) for row in db.execute(
                'SELECT id,role,kind,card,status,worker,registered_at FROM jobs ORDER BY sequence')]
            events = [dict(row) for row in db.execute(
                'SELECT sequence,time,event,job,detail FROM queue_events ORDER BY sequence')]
            db.rollback()
        finally:
            db.close()
    for event in events:
        event['detail'] = json.loads(event['detail'])
        event['time_utc'] = _iso(event['time'])
    lifecycle = []
    event_file = run / 'events.jsonl'
    if event_file.is_file():
        for line in event_file.read_text(encoding='utf-8').splitlines():
            if line.strip():
                item = json.loads(line)
                timestamp = _timestamp(item.get('time'))
                if timestamp is not None:
                    lifecycle.append({**item, 'timestamp': timestamp})
    by_job = defaultdict(list)
    for event in events:
        by_job[event['job']].append(event)
    attempts, model_records = [], []
    warnings = []
    end_events = {'delivered', 'late_image_delivered', 'abandoned', 'cancelled',
                  'lease_expired', 'lease_expired_retry_wait', 'uncertain_settled'}
    for job in jobs:
        request = _read(run / 'jobs' / job['id'] / 'request.json', {})
        stage = job['kind']
        if stage == 'image':
            edit = any(str(image.get('role', '')).startswith('edit_target')
                       for image in request.get('images', []))
            edit = edit or bool(request.get('context', {}).get('art_history'))
            stage = 'image_edit' if edit else 'image_initial'
        job['stage'] = stage
        job['registered_utc'] = _iso(job['registered_at'])
        timeline, active, ordinal = by_job[job['id']], None, 0
        for event in timeline:
            if event['event'] == 'claimed':
                if active is not None:
                    warnings.append(f"{job['id']}: repeated claim without a recorded previous end")
                    attempts.append(active)
                ordinal += 1
                active = {'job_id': job['id'], 'attempt': ordinal, 'card': job['card'],
                          'kind': job['kind'], 'stage': stage,
                          'worker': event['detail'].get('worker', 'unknown'),
                          'status': job['status'], 'registered_at': job['registered_at'],
                          'start': event['time'], 'start_utc': event['time_utc'],
                          'end': None, 'end_utc': None, 'seconds': None,
                          'model_seconds': None,
                          'source_start_event': event['sequence']}
            elif active is not None and event['event'] in end_events:
                worker = event['detail'].get('worker')
                if worker and worker != active['worker']:
                    continue
                active.update(end=event['time'], end_utc=event['time_utc'],
                              seconds=max(0, event['time'] - active['start']),
                              end_event=event['event'], source_end_event=event['sequence'])
                attempts.append(active)
                active = None
        if active is not None:
            attempts.append(active)
        record_path = run / 'jobs' / job['id'] / 'generation-record.json'
        record = _read(record_path)
        if record is not None:
            seconds = _timestamp(record.get('seconds'))
            if seconds is not None and seconds >= 0:
                model_records.append({'job_id': job['id'], 'card': job['card'],
                                      'worker': record.get('worker'), 'seconds': seconds,
                                      'tool': record.get('tool'),
                                      'received': record.get('received'),
                                      'source': record_path.relative_to(run).as_posix(),
                                      'model_start': None, 'model_end': None})
                explicit=_read(record_path.parent/'generation-tool-timing.json',record)
                call_start=_timestamp(explicit.get('started_at'));call_end=_timestamp(explicit.get('ended_at'))
                if call_start is not None and call_end is not None:
                    if call_end>=call_start and abs((call_end-call_start)-seconds)<1:
                        model_records[-1].update(model_start=call_start,model_end=call_end,
                                                model_start_utc=_iso(call_start),model_end_utc=_iso(call_end))
                    else:warnings.append(job['id']+': inconsistent explicit model-call timer; not positioned on timeline')
                # A single recorded model duration must not be duplicated onto
                # earlier expired/reclaimed attempts of the same queue job.
                delivered_attempt = next((row for row in reversed(attempts)
                    if row['job_id'] == job['id']
                    and row.get('end_event') in ('delivered', 'late_image_delivered')
                    and (not record.get('worker') or row['worker'] == record['worker'])), None)
                if delivered_attempt is not None:
                    delivered_attempt['model_seconds'] = seconds
                    delivered_attempt.update(model_start=model_records[-1]['model_start'],model_end=model_records[-1]['model_end'])
    attempts.sort(key=lambda row: (row['start'], row['job_id'], row['attempt']))
    complete = [row for row in attempts if row['end'] is not None]
    created = _timestamp(state.get('created') or state.get('created_utc'))
    terminals = [item for item in lifecycle if item['event'] in ('complete', 'completed_with_rejections')]
    terminal = terminals[-1]['timestamp'] if terminals else None
    all_times = [item['time'] for item in events] + [item['timestamp'] for item in lifecycle]
    start = created if created is not None else min(all_times, default=None)
    end = terminal if terminal is not None else max(all_times, default=None)
    occupied = _union([(row['start'], row['end']) for row in complete])
    gaps = []
    if start is not None and end is not None:
        cursor = start
        for left, right in occupied:
            left, right = max(left, start), min(right, end)
            if right < start or left > end:
                continue
            if left > cursor:
                gaps.append({'start': cursor, 'end': left, 'seconds': left-cursor,
                             'classification': 'unclassified_outside_recorded_worker_intervals'})
            cursor = max(cursor, right)
        if end > cursor:
            gaps.append({'start': cursor, 'end': end, 'seconds': end-cursor,
                         'classification': 'unclassified_outside_recorded_worker_intervals'})
    stages = []
    for stage in dict.fromkeys([*STAGES, *(row['stage'] for row in attempts)]):
        rows = [row for row in attempts if row['stage'] == stage]
        if not rows:
            continue
        finished = [row for row in rows if row['end'] is not None]
        first = min(row['start'] for row in rows)
        last = max((row['end'] for row in finished), default=None)
        stages.append({'stage': stage, 'label': STAGES.get(stage, stage),
                       'jobs': len({row['job_id'] for row in rows}), 'attempts': len(rows),
                       'completed_attempts': len(finished),
                       'busy_seconds': sum(row['seconds'] for row in finished),
                       'start': first, 'end': last,
                       'elapsed_seconds': last-first if last is not None else None})
    coordinator_intervals, export_active, export_attempt = [], None, 0
    for item in lifecycle:
        if item['event'] == 'export_started':
            if export_active is not None:
                coordinator_intervals.append(export_active)
                warnings.append('Export started again without a recorded previous end')
            export_attempt += 1
            export_active = {'stage': 'export', 'attempt': export_attempt,
                             'start': item['timestamp'], 'start_utc': _iso(item['timestamp']),
                             'end': None, 'end_utc': None, 'seconds': None,
                             'start_event': 'export_started', 'end_event': None}
        elif item['event'] in ('export_finished', 'export_failed', 'export_error'):
            if export_active is not None:
                export_active.update(end=item['timestamp'], end_utc=_iso(item['timestamp']),
                                     seconds=max(0, item['timestamp']-export_active['start']),
                                     end_event=item['event'])
                coordinator_intervals.append(export_active)
                export_active = None
    if export_active is not None:
        coordinator_intervals.append(export_active)
    # Explicit coordinator timers are known work, so do not also label their
    # duration as an unclassified gap outside worker leases.
    for interval in coordinator_intervals:
        if interval['end'] is None:
            continue
        remaining = []
        for gap in gaps:
            left, right = max(gap['start'], interval['start']), min(gap['end'], interval['end'])
            if left >= right:
                remaining.append(gap)
                continue
            for part_start, part_end in ((gap['start'], left), (right, gap['end'])):
                if part_end > part_start:
                    remaining.append({**gap, 'start': part_start, 'end': part_end,
                                      'seconds': part_end-part_start})
        gaps = remaining
    markers = [{'time': item['timestamp'], 'time_utc': _iso(item['timestamp']),
                'event': item['event'], 'detail': {k: v for k, v in item.items()
                if k not in ('timestamp', 'time')}}
               for item in lifecycle if item['event'] in (
                   'created', 'policy_amended', 'complete', 'completed_with_rejections',
                   'export_started', 'export_finished', 'export_failed', 'export_error')
               or (item['event'] == 'gate_result' and item.get('gate') == 'export')]
    amendments = [item['time'] for item in markers if item['event'] == 'policy_amended']
    phases = []
    if start is not None and end is not None:
        boundaries = [start, *[value for value in amendments if start < value < end], end]
        for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
            phase_rows = [row for row in complete if left <= row['start'] < right]
            work_end = min(right, max((row['end'] for row in phase_rows), default=left))
            # A pre-amendment gap is visible without attributing its cause to
            # a human, the model, or the coordinator.
            phase_end = work_end if index < len(boundaries)-2 else right
            phases.append({'label': 'Первый тест' if index == 0 else 'Продолжение после изменения правил',
                           'start': left, 'end': phase_end, 'elapsed_seconds': phase_end-left,
                           'worker_busy_seconds': sum(row['seconds'] for row in phase_rows),
                           'note': 'Промежутки вне записанных заданий не классифицированы как человеческое ожидание.'})
            if phase_end < right:
                phases.append({'label': 'Промежуток вне записанных заданий перед изменением правил',
                               'start': phase_end, 'end': right, 'elapsed_seconds': right-phase_end,
                               'worker_busy_seconds': 0, 'classification': 'unclassified',
                               'note': 'Причина промежутка не установлена по журналам очереди.'})
    return {'version': 1, 'timezone': 'Europe/Madrid', 'queue_present': path.is_file(),
            'sources': ['queue.sqlite3:jobs', 'queue.sqlite3:queue_events', 'events.jsonl',
                        'jobs/*/generation-record.json'],
            'definitions': {
                'worker_interval': 'claim→deliver (or recorded failure): includes input inspection, prompt work, tool execution, copying and delivery; not model-only time or CPU usage',
                'busy_sum': 'Sum of measured worker intervals; concurrent work is intentionally added and can exceed elapsed wall time',
                'elapsed': 'First start to last recorded end, including waits and gaps',
                'model_seconds': 'Recorded tool-call duration; explicit start/end used only where actually saved, never reconstructed from receipt/file dates',
                'gaps': 'Outside recorded worker and explicitly timed export intervals; may include other coordinator work, changes, review of results or idle time. Cause is not established',
                'critical_path': 'Not calculated: timestamps alone do not establish the complete dependency graph',
                'coordinator': 'Only explicit export_started→export_finished/failed/error pairs give export duration; other lifecycle events are markers, not inferred work'},
            'start': start, 'end': end, 'start_utc': _iso(start), 'end_utc': _iso(end),
            'elapsed_seconds': end-start if start is not None and end is not None else None,
            'worker_busy_seconds': sum(row['seconds'] for row in complete),
            'worker_occupied_wall_seconds': sum(right-left for left, right in occupied),
            'unclassified_seconds': sum(item['seconds'] for item in gaps),
            'model_seconds': sum(item['seconds'] for item in model_records),
            'job_count': len(jobs), 'attempt_count': len(attempts),
            'completed_attempt_count': len(complete), 'jobs': jobs,
            'attempts': attempts, 'stages': stages, 'gaps': gaps,
            'model_records': model_records, 'markers': markers, 'phases': phases,
            'coordinator_intervals': coordinator_intervals,
            'queue_events': events, 'warnings': warnings}


def render_timing(run, state):
    """Self-contained, prefixed HTML section suitable for appending to a report."""
    data = collect_timing(run, state)
    if data['start'] is None:
        return '<section class="sf-time"><h2>Время работы</h2><p>Нет записанных временных событий.</p></section>'
    begin, end = data['start'], data['end']
    span = max(1, end-begin)
    esc = lambda value: escape(str(value), quote=True)
    same_day=_local(begin)[:10]==_local(end)[:10]
    def stamp(value):
        if value is None:return '—'
        local=datetime.fromisoformat(_local(value))
        return local.strftime('%H:%M:%S' if same_day else '%d.%m.%Y %H:%M:%S')
    def x(value):
        return 100 * (value-begin) / span
    worker_ids = sorted({row['worker'] for row in data['attempts']},
                        key=lambda value: (0 if 'planner' in value else 1 if 'artist' in value else 2, value))
    artists=[w for w in worker_ids if any(r['worker']==w and r['kind']=='image' for r in data['attempts'])]
    def worker_name(worker):
        if worker in artists:
            label='Художник '+str(artists.index(worker)+1)
            return label+' / приёмка' if any(r['worker']==worker and r['kind'] in ('card_review','entity_review') for r in data['attempts']) else label
        kinds={r['kind'] for r in data['attempts'] if r['worker']==worker}
        return 'Планировщик' if kinds=={'plan'} else 'Арт-директор' if kinds and 'plan' not in kinds else worker
    from .naming import names,display_title
    mapping=names(run)
    card_names={v['id']+'/'+c['id']:display_title(mapping,v)+' · '+display_title(mapping,v,c)
                for v in (state.get('plan') or {}).get('variants',[]) for c in v['cards']}
    def card_name(key):return card_names.get(key,key or 'весь заказ')
    lane_html = []
    for worker in worker_ids:
        bars = []
        for row in data['attempts']:
            if row['worker'] != worker or row['end'] is None:
                continue
            title = (f"{row['job_id']} · {STAGES.get(row['stage'], row['stage'])} · "
                     f"{card_name(row['card'])} · попытка {row['attempt']}\n"
                     f"{stamp(row['start'])} → {stamp(row['end'])}\n"
                     f"Задание: {_duration(row['seconds'])}; конец: {row['end_event']}")
            bars.append(f'<a class="sf-time-bar" href="#sf-time-job-{esc(row["job_id"])}" title="{esc(title)}" '
                        f'style="left:{x(row["start"]):.5f}%;width:{max(.08,100*row["seconds"]/span):.5f}%;'
                        f'background:{COLORS.get(row["stage"], "#94a3b8")}"></a>')
        lane_html.append(f'<div class="sf-time-lane"><b title="{esc(worker)}">{esc(worker_name(worker))}</b><div class="sf-time-track">{"".join(bars)}</div></div>')
    markers = []
    labels = {'created': 'Заказ создан', 'policy_amended': 'Изменены правила; продолжение',
              'gate_result': 'Экспорт проверен', 'complete': 'Заказ завершён',
              'completed_with_rejections': 'Завершён с отклонениями',
              'export_started': 'Экспорт начат', 'export_finished': 'Экспорт завершён',
              'export_failed': 'Экспорт не удался', 'export_error': 'Ошибка экспорта'}
    for interval in data['coordinator_intervals']:
        if interval['end'] is not None:
            title = f"Экспорт, попытка {interval['attempt']}: {_duration(interval['seconds'])}; {interval['end_event']}"
            markers.append(f'<i class="sf-time-bar" title="{esc(title)}" '
                           f'style="left:{x(interval["start"]):.5f}%;width:{max(.08,100*interval["seconds"]/span):.5f}%;background:#cbd5e1"></i>')
    for marker in data['markers']:
        markers.append(f'<i class="sf-time-marker" style="left:{min(99.7,max(0,x(marker["time"]))):.5f}%" '
                       f'title="{esc(labels.get(marker["event"],marker["event"]))} · {esc(stamp(marker["time"]))}"></i>')
    lane_html.append('<div class="sf-time-lane"><b>Координатор: события</b><div class="sf-time-track">'+''.join(markers)+'</div></div>')
    stage_rows = ''.join(f'<tr><td>{esc(row["label"])}</td><td>{row["jobs"]} / {row["attempts"]}</td>'
                         f'<td>{_duration(row["busy_seconds"])}</td><td>{_duration(row["elapsed_seconds"])}</td></tr>'
                         for row in data['stages'])
    exports = [row for row in data['coordinator_intervals'] if row['end'] is not None]
    if exports:
        export_busy = sum(row['seconds'] for row in exports)
        export_span = max(row['end'] for row in exports)-min(row['start'] for row in exports)
        stage_rows += (f'<tr><td>Экспорт: явный таймер координатора</td><td>— / {len(exports)}</td>'
                       f'<td>{_duration(export_busy)}</td><td>{_duration(export_span)}</td></tr>')
        export_note = f'Экспорт: {len(exports)} записанных завершённых попыток, суммарно {_duration(export_busy)} по явным start/end-событиям. Ошибочные попытки также сохранены.'
    else:
        export_note = 'Экспорт и завершение показаны только событиями: отдельный таймер экспорта в этом запуске не записан.'
    export_table = ''
    if data['coordinator_intervals']:
        export_rows = ''.join(f'<tr><td>{row["attempt"]}</td><td>{esc(stamp(row["start"]))}</td>'
                              f'<td>{esc(stamp(row["end"]))}</td><td>{_duration(row["seconds"])}</td>'
                              f'<td>{esc(row["end_event"] or "конец не записан")}</td></tr>'
                              for row in data['coordinator_intervals'])
        export_table = ('<details><summary>Записанные попытки экспорта</summary><div class="sf-time-scroll"><table>'
                        '<thead><tr><th>Попытка</th><th>Начало</th><th>Конец</th><th>Длительность</th><th>Результат</th></tr></thead>'
                        f'<tbody>{export_rows}</tbody></table></div></details>')
    busiest = max(data['stages'], key=lambda row: row['busy_seconds'], default=None)
    interpretation = (f'Больше всего измеренной занятости: <b>{esc(busiest["label"])}</b> — '
                      f'{busiest["busy_seconds"]/60:.1f} мин, {busiest["attempts"]} попыток. '
                      if busiest else 'Нет завершённых рабочих интервалов для сравнения этапов. ')
    interpretation += (f'Отдельно записанные вызовы генерации заняли {data["model_seconds"]/60:.1f} мин '
                       f'({len(data["model_records"])} записей). Это часть работы художников, не дополнительное время поверх неё. '
                       'Эти измерения показывают распределение времени, но сами по себе не устанавливают причины задержек.')
    rows = []
    for row in data['attempts']:
        model_cell=_duration(row['model_seconds'])
        if row.get('model_start') is not None and row.get('model_end') is not None:
            model_cell+=f'<br><small>{esc(stamp(row["model_start"]))} → {esc(stamp(row["model_end"]))}</small>'
        rows.append(f'<tr id="sf-time-job-{esc(row["job_id"])}"><td>{esc(row["job_id"])} / {row["attempt"]}</td>'
                    f'<td title="{esc(row["worker"])}">{esc(worker_name(row["worker"]))}</td><td>{esc(STAGES.get(row["stage"],row["stage"]))}<br>{esc(card_name(row["card"]))}</td>'
                    f'<td>{esc(stamp(row["start"]))}</td><td>{esc(stamp(row["end"]))}</td>'
                    f'<td>{_duration(row["seconds"])}</td><td>{model_cell}</td>'
                    f'<td>{esc(row.get("end_event", "ещё нет конца"))}</td></tr>')
    phase_html = ''.join(f'<li>{esc(row["label"])}: {esc(stamp(row["start"]))} → {esc(stamp(row["end"]))}; '
                         f'{_duration(row["elapsed_seconds"])} по часам.</li>' for row in data['phases'])
    gaps = sorted(data['gaps'], key=lambda row: row['seconds'], reverse=True)[:5]
    gap_html = ''.join(f'<li>{esc(stamp(row["start"]))} → {esc(stamp(row["end"]))}: {_duration(row["seconds"])}.</li>' for row in gaps)
    legend = ''.join(f'<span><i style="background:{COLORS[key]}"></i>{esc(label)}</span>' for key,label in STAGES.items())
    exact=sum(row['model_start'] is not None and row['model_end'] is not None for row in data['model_records'])
    timing_note=(f'Точные начало и конец сохранены для {exact} из {len(data["model_records"])} вызовов генерации; они указаны в хронологии заданий.' if exact else 'Точные начало и конец вызовов модели не записаны: известны только длительности инструментов.')
    return f'''<section class="sf-time" id="sf-time-analysis">
<style>
.sf-time{{margin:40px 0 0;padding:28px;background:#101827;color:#eef2ff;border-radius:18px;font:15px/1.5 system-ui,sans-serif;overflow:hidden}}
.sf-time h2,.sf-time h3{{color:#fff;margin:0 0 12px}}.sf-time h3{{margin-top:24px}}.sf-time a{{color:#93c5fd}}.sf-time p{{max-width:1100px}}
.sf-time-metrics{{display:flex;gap:12px;flex-wrap:wrap}}.sf-time-metrics div{{background:#1e293b;padding:14px;border-radius:10px;flex:1;min-width:170px}}.sf-time-metrics strong{{display:block;font-size:23px;color:#fff}}
.sf-time-scroll{{overflow-x:auto}}.sf-time-chart{{min-width:950px;margin:20px 0}}.sf-time-axis{{margin-left:190px;display:flex;justify-content:space-between;font-size:12px;color:#cbd5e1}}
.sf-time-lane{{display:grid;grid-template-columns:190px 1fr;align-items:center;min-height:48px;border-bottom:1px solid #334155}}.sf-time-lane>b{{font-size:12px;padding-right:10px;overflow-wrap:anywhere}}
.sf-time-track{{height:34px;position:relative;background:repeating-linear-gradient(to right,#33415555 0,#33415555 1px,transparent 1px,transparent 10%)}}
.sf-time-bar{{position:absolute;top:7px;height:20px;min-width:2px;border-radius:3px;box-shadow:0 0 0 1px #101827}}.sf-time-bar:hover,.sf-time-bar:focus{{outline:2px solid white;z-index:3}}
.sf-time-marker{{position:absolute;top:9px;width:10px;height:16px;background:#fff;transform:translateX(-50%);clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%)}}
.sf-time-legend{{display:flex;gap:10px 18px;flex-wrap:wrap;font-size:12px}}.sf-time-legend span{{display:flex;align-items:center;gap:5px}}.sf-time-legend i{{width:12px;height:12px;border-radius:3px}}
.sf-time table{{background:transparent;border-collapse:collapse;width:100%;color:inherit;font-size:13px}}.sf-time th,.sf-time td{{padding:9px;text-align:left;border-bottom:1px solid #334155;vertical-align:top}}.sf-time th{{background:#1e293b;color:#fff}}.sf-time tr:target{{background:#304668}}
.sf-time details{{margin-top:20px}}.sf-time summary{{cursor:pointer;font-weight:700}}.sf-time small{{color:#cbd5e1}}
</style>
<h2>Время работы: что происходило и сколько заняло</h2>
<p>Дата: {datetime.fromisoformat(_local(begin)).strftime('%d.%m.%Y')}. Часовой пояс — Europe/Madrid. Период заказа: {esc(stamp(begin))} → {esc(stamp(end))}. График построен по реальным событиям очереди, с сохранением параллельной работы и повторных попыток.</p>
<div class="sf-time-metrics"><div>От создания до завершения<strong>{_duration(data['elapsed_seconds'])}</strong></div>
<div>Занятость исполнителей, сумма<strong>{_duration(data['worker_busy_seconds'])}</strong></div>
<div>Вызовы генерации, отдельно<strong>{_duration(data['model_seconds'])}</strong></div>
<div>Задания / попытки<strong>{data['job_count']} / {data['attempt_count']}</strong></div></div>
<p><b>Как читать время.</b> Полоса — от взятия задания до доставки результата: просмотр входов, работа с промптом, вызов инструмента, копирование и отправка. Это не чистое время модели. Сумма занятости складывает параллельные интервалы; календарное время не складывается. {timing_note}</p>
<h3>Работа по исполнителям</h3><div class="sf-time-scroll"><div class="sf-time-chart"><div class="sf-time-axis">{''.join('<span>'+esc(stamp(begin+span*i/4))+'</span>' for i in range(5))}</div>{''.join(lane_html)}</div></div>
<div class="sf-time-legend">{legend}</div><p><small>Наведите на полосу для деталей; нажмите, чтобы перейти к заданию. Ромбы координатора — зафиксированные события, не измеренная длительность работы.</small></p>
<h3>По этапам</h3><div class="sf-time-scroll"><table><thead><tr><th>Этап</th><th>Задания / попытки</th><th>Занятость, сумма</th><th>От первого старта до последнего конца</th></tr></thead><tbody>{stage_rows}</tbody></table></div>
<p>{interpretation}</p>
<h3>Последовательность прогона</h3><ul>{phase_html}</ul>
<p>Вне записанной работы исполнителей и явно измеренного экспорта: <b>{_duration(data['unclassified_seconds'])}</b>. Это неклассифицированные промежутки: в них могли входить другая работа координатора, изменения правил и инструмента, просмотр результатов или простой. Называть всё это ожиданием человека нельзя. Крупнейшие промежутки:</p><ul>{gap_html}</ul>
<p>{esc(export_note)} Критический путь не вычислялся: одних временных меток недостаточно для полного графа зависимостей. Очередь сохраняет claim/deliver/acknowledged; генераторы отдельно сохраняют длительность инструмента. Границы вызова модели используются только из явных start/end; отсутствующие времена не вычисляются из доставки или дат файлов.</p>
{export_table}
<details class="sf-time-jobs"><summary>Все {data['attempt_count']} попыток в хронологическом порядке</summary><div class="sf-time-scroll"><table><thead><tr><th>Задание / попытка</th><th>Исполнитель</th><th>Этап / карточка</th><th>Начало</th><th>Конец</th><th>Всё задание</th><th>Инструмент генерации</th><th>Конечное событие</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
<p><a href="timing-data.json">Исходные временные данные JSON</a> · Источники: queue.sqlite3 / queue_events, events.jsonl, generation-record.json. История карточек и названия не изменены.</p>
<script>(()=>{{const root=document.currentScript.parentElement;root.querySelectorAll('a.sf-time-bar').forEach(bar=>bar.addEventListener('click',()=>{{const details=root.querySelector('.sf-time-jobs');if(details)details.open=true;}}));}})();</script>
</section>'''
