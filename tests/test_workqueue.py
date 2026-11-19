"""Durability and ambiguous-attempt checks, with no model or network calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from setforge.workqueue import WorkQueue


class QueueDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'run'
        self.run.mkdir()
        self.queue = WorkQueue(self.run)
        self.queue.configure(1)

    def register(self, n=1, role='image'):
        kind = 'image' if role == 'image' else 'card_review'
        job = f'{n:04d}-{kind}'
        self.queue.register({'id': job, 'role': role, 'task_key': kind + ':set/card' + str(n),
                             'request_sha256': str(n).zfill(64)}, card='set/card' + str(n), kind=kind)
        return job

    def test_quarantine_immediately_holds_capacity_and_accepts_original_late_result(self):
        job = self.register()
        self.register(2)
        lease = self.queue.claim('artist-a', 'image')
        held = self.queue.quarantine(job, 'artist-a', lease['token'], 'Fixture timeout after dispatch')
        self.assertEqual(held['status'], 'uncertain')
        self.assertIsNone(held['error'])
        self.assertIn('after dispatch', held['uncertain_reason'])
        self.assertEqual(held['token'], lease['token'])
        self.assertEqual(self.queue.inbox(), [])
        self.assertIsNone(self.queue.claim('artist-b', 'image'))
        result = self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'late.png'})
        self.assertEqual(result['status'], 'submitted')
        self.queue.acknowledge(job)
        self.assertEqual(self.queue.claim('artist-b', 'image')['id'], '0002-image')

    def test_quarantine_cannot_change_another_owner_review_or_delivered_result(self):
        job = self.register()
        lease = self.queue.claim('artist-a', 'image')
        with self.assertRaises(ValueError):
            self.queue.quarantine(job, 'artist-b', lease['token'], 'Fixture foreign owner')
        self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'complete.png'})
        with self.assertRaises(ValueError):
            self.queue.quarantine(job, 'artist-a', lease['token'], 'Fixture delivery already complete')
        review_job = self.register(2, role='reviewer')
        review = self.queue.claim('director', 'reviewer')
        with self.assertRaises(ValueError):
            self.queue.quarantine(review_job, 'director', review['token'], 'Fixture review timeout')

    def test_old_worker_cannot_override_operator_reconciliation_before_or_after_ack(self):
        job = self.register()
        lease = self.queue.claim('artist-a', 'image')
        self.queue.quarantine(job, 'artist-a', lease['token'], 'Fixture ambiguous call')
        settled = self.queue.settle_uncertain(job, 'Fixture process stopped and no result exists')
        self.assertEqual(settled['status'], 'submitted')
        self.assertIsNone(settled['response'])
        for acknowledged in (False, True):
            if acknowledged:
                self.queue.acknowledge(job, 'error')
            with self.assertRaises(ValueError):
                self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'unexpected.png'})
            with self.assertRaises(ValueError):
                self.queue.quarantine(job, 'artist-a', lease['token'], 'Fixture stale worker')

    def test_cancelled_executing_image_keeps_capacity_until_late_pixels_are_discarded(self):
        job = self.register()
        self.register(2)
        lease = self.queue.claim('artist-a', 'image')
        held = self.queue.cancel(job, 'Fixture old plan invalidated')
        self.assertEqual(held['status'], 'uncertain')
        self.assertEqual(held['token'], lease['token'])
        self.assertEqual(held['worker'], 'artist-a')
        self.assertIn('old plan', held['cancel_reason'])
        self.assertIsNone(self.queue.claim('artist-b', 'image'))
        self.assertEqual(self.queue.inbox(), [])
        late = self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'discarded-old-plan.png'})
        self.assertEqual(late['status'], 'submitted')
        discarded = self.queue.cancel(job, 'Fixture coordinator discards result from cancelled main job')
        self.assertEqual(discarded['status'], 'cancelled')
        self.assertEqual(discarded['response'], {'image_path': 'discarded-old-plan.png'})
        self.assertIsNone(discarded['token'])
        self.assertEqual(self.queue.claim('artist-b', 'image')['id'], '0002-image')
        with self.assertRaises(ValueError):
            self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'discarded-old-plan.png'})

    def test_cancelled_executing_image_can_be_reconciled_without_generation_or_acceptance(self):
        job = self.register()
        lease = self.queue.claim('artist-a', 'image')
        self.queue.cancel(job, 'Fixture cancelled plan')
        self.queue.settle_uncertain(job, 'Fixture original worker stopped; no output')
        discarded = self.queue.cancel(job, 'Fixture coordinator discards old attempt')
        self.assertEqual(discarded['status'], 'cancelled')
        self.assertIsNone(discarded['response'])
        self.assertIn('Operator confirmed', discarded['error'])
        self.assertIsNone(discarded['token'])

    def test_ready_and_submitted_images_and_reasoning_jobs_cancel_without_held_reservation(self):
        ready = self.register()
        self.assertEqual(self.queue.cancel(ready, 'Fixture never dispatched')['status'], 'cancelled')
        submitted = self.register(2)
        lease = self.queue.claim('artist-a', 'image')
        self.queue.deliver(submitted, 'artist-a', lease['token'], {'image_path': 'already-finished.png'})
        self.assertEqual(self.queue.cancel(submitted, 'Fixture result not needed')['status'], 'cancelled')
        review = self.register(3, role='reviewer')
        lease = self.queue.claim('director', 'reviewer')
        cancelled = self.queue.cancel(review, 'Fixture review invalidated')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(cancelled['token'])

    def test_expired_heartbeat_persists_quarantine_even_when_rejected(self):
        job = self.register()
        lease = self.queue.claim('artist-a', 'image')
        with closing(sqlite3.connect(self.queue.path)) as db:
            db.execute('UPDATE jobs SET expires_at=? WHERE id=?', (time.time() - 1, job))
            db.commit()
        with self.assertRaises(ValueError):
            self.queue.heartbeat(job, 'artist-a', lease['token'])
        self.assertEqual(self.queue.jobs()[0]['status'], 'uncertain')

    def test_managed_reasoning_expiry_waits_for_coordinator_budget_reservation(self):
        self.queue.configure(artists=2, managed_retries=True)
        job = self.register(role='reviewer')
        lease = self.queue.claim('director-a', 'reviewer')
        with closing(sqlite3.connect(self.queue.path)) as db:
            db.execute('UPDATE jobs SET expires_at=? WHERE id=?', (time.time() - 1, job))
            db.commit()
        self.assertIsNone(self.queue.claim('director-b', 'reviewer'))
        waiting = self.queue.jobs()[0]
        self.assertEqual(waiting['status'], 'retry_wait')
        self.assertIsNone(waiting['worker'])
        self.assertIsNone(waiting['token'])
        with self.assertRaises(ValueError):
            self.queue.deliver(job, 'director-a', lease['token'], {'verdict': 'stale fixture'})
        self.assertTrue(self.queue.configure(artists=8)['managed_retries'])
        self.assertEqual(self.queue.cancel(job, 'Fixture coordinator records expired attempt')['status'], 'cancelled')

    def test_standalone_reasoning_expiry_retains_legacy_reclaim_behavior(self):
        job = self.register(role='reviewer')
        lease = self.queue.claim('director-a', 'reviewer')
        with closing(sqlite3.connect(self.queue.path)) as db:
            db.execute('UPDATE jobs SET expires_at=? WHERE id=?', (time.time() - 1, job))
            db.commit()
        reclaimed = self.queue.claim('director-b', 'reviewer')
        self.assertEqual(reclaimed['id'], job)
        self.assertNotEqual(reclaimed['token'], lease['token'])

    def test_artist_identity_cannot_review_its_own_card_but_can_claim_another(self):
        self.queue.configure(2)
        self.register()
        self.register(2)
        a = self.queue.claim('artist-a', 'image')
        b = self.queue.claim('artist-b', 'image')
        for lease in (a, b):
            self.queue.deliver(lease['id'], lease['worker'], lease['token'], {'image_path': lease['worker'] + '.png'})
            self.queue.acknowledge(lease['id'])
        for n, card in ((3, 'set/card1'), (4, 'set/card2')):
            self.queue.register({'id': f'{n:04d}-card_review', 'role': 'reviewer',
                                 'task_key': 'card_review:' + card, 'request_sha256': str(n)},
                                card=card, kind='card_review')
        other = self.queue.claim('artist-a', 'reviewer')
        self.assertEqual(other['id'], '0004-card_review')
        self.queue.deliver(other['id'], 'artist-a', other['token'], {'verdict': 'fixture'})
        self.queue.acknowledge(other['id'])
        self.assertIsNone(self.queue.claim('artist-a', 'reviewer'))
        own = self.queue.claim('director', 'reviewer')
        self.assertEqual(own['id'], '0003-card_review')
        self.queue.deliver(own['id'], 'director', own['token'], {'verdict': 'fixture'})
        self.queue.acknowledge(own['id'])
        self.queue.register({'id': '0005-brief_review', 'role': 'reviewer',
                             'task_key': 'brief_review:brief_review', 'request_sha256': '5'}, kind='brief_review')
        self.assertEqual(self.queue.claim('artist-a', 'reviewer')['id'], '0005-brief_review')

    def test_backup_is_standalone_consistent_and_does_not_touch_production_state(self):
        job = self.register()
        lease = self.queue.claim('artist-a', 'image')
        self.queue.deliver(job, 'artist-a', lease['token'], {'image_path': 'fixture.png'})
        state = self.run / 'state.json'
        state.write_text('unchanged fixture', encoding='utf-8')
        destination = self.run.parent / 'snapshot' / 'queue.sqlite3'
        # backup() intentionally returns the canonical path; Windows TEMP may
        # spell the same directory with a short name such as RUNNER~1.
        self.assertEqual(self.queue.backup(destination), destination.resolve())
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0], 'submitted')
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertFalse(Path(str(destination) + '-wal').exists())
        self.assertEqual(state.read_text(encoding='utf-8'), 'unchanged fixture')
        for protected in (self.queue.path, Path(str(self.queue.path) + '-wal'), Path(str(self.queue.path) + '-shm')):
            with self.assertRaises(ValueError):
                self.queue.backup(protected)

    def test_backup_during_writes_contains_a_valid_committed_prefix(self):
        self.register()
        destination = self.run.parent / 'concurrent-snapshot.sqlite3'
        def register_more():
            for n in range(2, 40):
                self.register(n)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(register_more)
            self.queue.backup(destination)
            future.result()
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            numbers = [row[0] for row in db.execute('SELECT sequence FROM jobs ORDER BY sequence')]
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
            self.assertTrue(1 <= len(numbers) <= 39)
        self.assertEqual(len(self.queue.jobs()), 39)


if __name__ == '__main__':
    unittest.main()
