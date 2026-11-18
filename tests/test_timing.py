"""Audit real queue intervals without invoking or mutating production workers."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from setforge.timing import collect_timing, render_timing, _local


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.epoch = datetime(2026, 11, 17, 12, tzinfo=timezone.utc).timestamp()
        self.state = {'created': datetime.fromtimestamp(self.epoch, timezone.utc).isoformat()}
        self.db = sqlite3.connect(self.run / 'queue.sqlite3')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE jobs(sequence INTEGER PRIMARY KEY,id TEXT,role TEXT,
                              kind TEXT,card TEXT,status TEXT,worker TEXT,registered_at REAL);
            CREATE TABLE queue_events(sequence INTEGER PRIMARY KEY,time REAL,event TEXT,job TEXT,detail TEXT);
        ''')
        self.sequence = 0

    def job(self, key, kind='image', worker='artist-a', card='set/card', status='done', edit=False):
        self.db.execute('INSERT INTO jobs(id,role,kind,card,status,worker,registered_at) VALUES (?,?,?,?,?,?,?)',
                        (key, 'image' if kind == 'image' else 'reviewer', kind, card,
                         status, worker, self.epoch))
        folder = self.run / 'jobs' / key
        folder.mkdir(parents=True)
        (folder / 'request.json').write_text(json.dumps({'images': [{'role': 'edit_target:previous'}]
                                                        if edit else []}), encoding='utf-8')
        return folder

    def event(self, key, kind, offset, **detail):
        self.sequence += 1
        self.db.execute('INSERT INTO queue_events VALUES (?,?,?,?,?)',
                        (self.sequence, self.epoch + offset, kind, key, json.dumps(detail)))
        self.db.commit()

    def lifecycle(self, *items):
        with (self.run / 'events.jsonl').open('w', encoding='utf-8') as stream:
            for offset, event in items:
                stream.write(json.dumps({'time': datetime.fromtimestamp(
                    self.epoch + offset, timezone.utc).isoformat(), **event}) + '\n')

    def test_parallel_sum_differs_from_elapsed_and_gaps_are_unclassified(self):
        self.job('a')
        self.job('b', worker='artist-b', card='set/other', edit=True)
        self.event('a', 'claimed', 10, worker='artist-a')
        self.event('b', 'claimed', 20, worker='artist-b')
        self.event('a', 'delivered', 50, worker='artist-a')
        self.event('b', 'delivered', 60, worker='artist-b')
        self.lifecycle((0, {'event': 'created'}), (100, {'event': 'complete'}))
        data = collect_timing(self.run, self.state)
        self.assertEqual(data['worker_busy_seconds'], 80)
        self.assertEqual(data['worker_occupied_wall_seconds'], 50)
        self.assertEqual(data['elapsed_seconds'], 100)
        self.assertEqual(data['unclassified_seconds'], 50)
        self.assertEqual([row['stage'] for row in data['attempts']], ['image_initial', 'image_edit'])
        self.assertEqual([row['classification'] for row in data['gaps']],
                         ['unclassified_outside_recorded_worker_intervals'] * 2)
        self.assertEqual(data['stages'][0]['elapsed_seconds'], 40)

    def test_reclaimed_reasoning_job_keeps_both_attempts_and_missing_end(self):
        self.job('r', kind='card_review', worker='reviewer')
        self.event('r', 'claimed', 1, worker='old-reviewer')
        self.event('r', 'lease_expired', 10, worker='old-reviewer')
        self.event('r', 'claimed', 20, worker='new-reviewer')
        self.event('r', 'delivered', 30, worker='new-reviewer')
        self.job('pending', status='leased')
        self.event('pending', 'claimed', 40, worker='artist-a')
        data = collect_timing(self.run, self.state)
        self.assertEqual(data['job_count'], 2)
        self.assertEqual(data['attempt_count'], 3)
        self.assertEqual(data['completed_attempt_count'], 2)
        self.assertEqual([row['attempt'] for row in data['attempts']], [1, 2, 1])
        self.assertEqual(data['worker_busy_seconds'], 19)
        self.assertIsNone(data['attempts'][-1]['end'])
        self.assertIsNone(data['attempts'][-1]['seconds'])

    def test_model_duration_is_separate_and_exact_call_times_not_invented(self):
        folder = self.job('a')
        self.event('a', 'claimed', 10, worker='artist-a')
        self.event('a', 'delivered', 100, worker='artist-a')
        (folder / 'generation-record.json').write_text(json.dumps(
            {'seconds': 22.25, 'worker': 'artist-a', 'received': '2026-11-17T12:01:30Z',
             'tool': 'built-in imagegen'}), encoding='utf-8')
        data = collect_timing(self.run, self.state)
        self.assertEqual(data['worker_busy_seconds'], 90)
        self.assertEqual(data['model_seconds'], 22.25)
        self.assertIsNone(data['model_records'][0]['model_start'])
        self.assertIsNone(data['model_records'][0]['model_end'])
        self.assertEqual(data['model_records'][0]['source'], 'jobs/a/generation-record.json')
        self.assertEqual(data['attempts'][0]['model_seconds'], 22.25)

    def test_uncertain_image_remains_open_until_explicit_settlement(self):
        self.job('a', status='error')
        self.event('a', 'claimed', 1, worker='artist-a')
        self.event('a', 'image_quarantined', 10, worker='artist-a')
        self.event('a', 'image_cancellation_held', 20)
        self.assertIsNone(collect_timing(self.run, self.state)['attempts'][0]['end'])
        self.event('a', 'uncertain_settled', 30)
        row = collect_timing(self.run, self.state)['attempts'][0]
        self.assertEqual(row['seconds'], 29)
        self.assertEqual(row['end_event'], 'uncertain_settled')

    def test_explicit_tool_boundaries_are_preserved_and_shown_without_inference(self):
        folder=self.job('a');self.event('a','claimed',10,worker='artist-a');self.event('a','delivered',100,worker='artist-a')
        (folder/'generation-record.json').write_text(json.dumps({'worker':'artist-a','seconds':20}),encoding='utf8')
        stamp=lambda delta:datetime.fromtimestamp(self.epoch+delta,timezone.utc).isoformat()
        (folder/'generation-tool-timing.json').write_text(json.dumps({'started_at':stamp(30),'ended_at':stamp(50)}),encoding='utf8')
        data=collect_timing(self.run,self.state)
        self.assertEqual(data['model_records'][0]['model_start'],self.epoch+30)
        self.assertEqual(data['attempts'][0]['model_end'],self.epoch+50)
        self.assertIn('сохранены для 1 из 1',render_timing(self.run,self.state))

    def test_invalid_tool_boundaries_are_not_accepted_as_exact_measurements(self):
        folder=self.job('a');self.event('a','claimed',10,worker='artist-a');self.event('a','delivered',100,worker='artist-a')
        (folder/'generation-record.json').write_text(json.dumps({'worker':'artist-a','seconds':20,'started_at':self.epoch+60,'ended_at':self.epoch+30}),encoding='utf8')
        data=collect_timing(self.run,self.state)
        self.assertIsNone(data['model_records'][0]['model_start'])
        self.assertEqual(data['model_seconds'],20)
        self.assertTrue(data['warnings'])

    def test_read_only_does_not_expire_or_rewrite_queue(self):
        self.job('expired', status='leased')
        self.event('expired', 'claimed', 1, worker='artist-a')
        path = self.run / 'queue.sqlite3'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        data = collect_timing(self.run, self.state)
        render_timing(self.run, self.state)
        self.assertEqual(data['jobs'][0]['status'], 'leased')
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue_events').fetchone()[0], 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertFalse((self.run / 'timing-data.json').exists())

    def test_pause_is_visible_without_claiming_human_wait_or_export_duration(self):
        self.job('a')
        self.event('a', 'claimed', 1, worker='artist-a')
        self.event('a', 'delivered', 10, worker='artist-a')
        self.job('b', worker='artist-b')
        self.event('b', 'claimed', 110, worker='artist-b')
        self.event('b', 'delivered', 120, worker='artist-b')
        self.lifecycle((0, {'event': 'created'}), (100, {'event': 'policy_amended'}),
                       (130, {'event': 'gate_result', 'gate': 'export', 'verdict': 'pass'}),
                       (131, {'event': 'complete'}))
        data = collect_timing(self.run, self.state)
        self.assertEqual(len(data['phases']), 3)
        pause = data['phases'][1]
        self.assertEqual(pause['classification'], 'unclassified')
        self.assertEqual(pause['elapsed_seconds'], 90)
        self.assertFalse(any(row['stage'] == 'export' for row in data['stages']))
        self.assertEqual(data['markers'][-2]['detail']['gate'], 'export')

    def test_render_escapes_identity_and_has_no_external_dependencies(self):
        self.job('a', worker='<script>bad()</script>')
        self.event('a', 'claimed', 1, worker='<script>bad()</script>')
        self.event('a', 'delivered', 10, worker='<script>bad()</script>')
        html = render_timing(self.run, self.state)
        self.assertNotIn('<script>bad()</script>', html)
        self.assertIn('&lt;script&gt;bad()', html)
        self.assertIn('sf-time-track', html)
        self.assertIn('Координатор: события', html)
        self.assertIn('timing-data.json', html)
        self.assertNotIn('https://', html)
        self.assertIn('details.open=true', html)
        self.assertIn('background:transparent', html)

    def test_explicit_export_pairs_include_failed_attempt_without_fake_spans(self):
        self.lifecycle((0, {'event': 'created'}),
                       (10, {'event': 'export_started'}), (14, {'event': 'export_failed'}),
                       (15, {'event': 'export_error'}),
                       (20, {'event': 'export_started'}), (27, {'event': 'export_finished'}),
                       (28, {'event': 'complete'}))
        data = collect_timing(self.run, self.state)
        self.assertEqual([row['seconds'] for row in data['coordinator_intervals']], [4, 7])
        self.assertEqual([row['end_event'] for row in data['coordinator_intervals']],
                         ['export_failed', 'export_finished'])
        self.assertEqual(data['job_count'], 0)
        self.assertEqual(data['worker_busy_seconds'], 0)
        self.assertEqual(data['unclassified_seconds'], 17)
        html = render_timing(self.run, self.state)
        self.assertIn('Экспорт: явный таймер координатора', html)
        self.assertIn('Записанные попытки экспорта', html)
        self.assertNotIn('отдельный таймер экспорта в этом запуске не записан', html)

    def test_unpaired_export_events_do_not_invent_duration(self):
        self.lifecycle((0, {'event': 'created'}), (10, {'event': 'export_error'}),
                       (20, {'event': 'export_started'}))
        data = collect_timing(self.run, self.state)
        self.assertEqual(len(data['coordinator_intervals']), 1)
        self.assertIsNone(data['coordinator_intervals'][0]['end'])
        self.assertIsNone(data['coordinator_intervals'][0]['seconds'])

    def test_local_time_handles_madrid_winter_and_summer(self):
        winter = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()
        summer = datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(_local(winter), '2026-01-01T13:00:00+01:00')
        self.assertEqual(_local(summer), '2026-07-01T14:00:00+02:00')

    def test_no_queue_does_not_create_one(self):
        self.db.close()
        (self.run / 'queue.sqlite3').unlink()
        data = collect_timing(self.run, self.state)
        self.assertFalse(data['queue_present'])
        self.assertEqual(data['attempts'], [])
        self.assertFalse((self.run / 'queue.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
