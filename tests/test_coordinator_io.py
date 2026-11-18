"""Coordinator file durability and portable queue snapshots; no production calls."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import zipfile

from setforge.engine import NeedsInput, Pipeline
from setforge.io import read, write
from setforge.parallel import enable
import test_pipeline as legacy


class CoordinatorIOTests(unittest.TestCase):
    setUp = legacy.PipelineTests.setUp

    def test_atomic_write_survives_two_transient_reader_locks_without_exposing_partial_json(self):
        target = self.root / 'durable.json'
        before = {'version': 'old', 'nested': {'list': [1, 2]}}
        after = {'version': 'new', 'nested': {'list': list(range(100)), 'text': 'complete replacement'}}
        write(target, before)
        replace = os.replace
        attempts = []
        def temporary_reader_lock(source, destination):
            attempts.append((source, destination))
            self.assertEqual(read(target), before)
            self.assertEqual(json.loads(Path(source).read_text(encoding='utf-8')), after)
            if len(attempts) < 3:
                raise PermissionError('Fixture temporary Windows delete-sharing lock')
            return replace(source, destination)
        with patch('setforge.io.os.replace', side_effect=temporary_reader_lock), patch('setforge.io.time.sleep'):
            write(target, after)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(read(target), after)
        self.assertFalse(target.with_name(target.name + '.tmp').exists())

    def test_read_retries_transient_permission_errors_but_remains_bounded(self):
        target = self.root / 'readable.json'
        expected = {'completed': True, 'number': 7}
        write(target, expected)
        read_text = Path.read_text
        attempts = []
        def temporary_read_lock(path, *args, **kwargs):
            if path == target:
                attempts.append(path)
                if len(attempts) < 3:
                    raise PermissionError('Fixture temporary Windows read lock')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', autospec=True, side_effect=temporary_read_lock), patch('setforge.io.time.sleep'):
            self.assertEqual(read(target), expected)
        self.assertEqual(len(attempts), 3)
        with patch.object(Path, 'read_text', side_effect=PermissionError('Fixture persistent lock')) as blocked, patch('setforge.io.time.sleep'):
            with self.assertRaises(PermissionError):
                read(target)
        self.assertGreater(blocked.call_count, 1)
        self.assertLessEqual(blocked.call_count, 8)

    def test_configuration_is_read_only_and_preserves_managed_mode(self):
        pipe = enable(Pipeline.create(self.project, self.brief, self.run), artists=8)
        queue = pipe.queue
        with closing(sqlite3.connect(queue.path)) as db:
            count = db.execute('SELECT COUNT(*) FROM queue_events').fetchone()[0]
        expected={'artists':8,'reviewers':2,'workers':9,'managed_retries':True}
        self.assertEqual(queue.configuration(), expected)
        self.assertEqual(queue.configuration(), expected)
        with closing(sqlite3.connect(queue.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM queue_events').fetchone()[0], count)

    def test_cli_pack_contains_a_standalone_queue_snapshot_including_wal_changes(self):
        from setforge.cli import main
        pipe = enable(Pipeline.create(self.project, self.brief, self.run), artists=2)
        with self.assertRaises(NeedsInput):
            pipe.advance()
        queue = pipe.queue
        expected_job = queue.jobs()[0]['id']
        archive_path = self.root / 'queued-run.zip'
        # A live reader keeps a WAL around while a worker commits its claim.
        with closing(sqlite3.connect(queue.path)) as live:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('SELECT COUNT(*) FROM jobs').fetchone()
            lease = queue.claim('fixture-director', 'reviewer')
            self.assertEqual(lease['id'], expected_job)
            self.assertTrue(Path(str(queue.path) + '-wal').exists())
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['pack', '--run', str(self.run), '--out', str(archive_path)]), 0)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist().count('queue.sqlite3'), 1)
            self.assertFalse(any(name.endswith(('-wal', '-shm')) for name in archive.namelist()))
            extracted = Path(archive.extract('queue.sqlite3', self.root / 'unpacked'))
        with closing(sqlite3.connect(extracted)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT id,status FROM jobs').fetchone(), (expected_job, 'leased'))
        self.assertEqual(queue.jobs()[0]['token'], lease['token'])


if __name__ == '__main__':
    unittest.main()
