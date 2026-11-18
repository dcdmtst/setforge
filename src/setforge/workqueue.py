"""Durable, provider-neutral leases and result inbox for external workers.

Only this database is written by workers.  Production state, policy decisions and
artifact acceptance remain the coordinator's responsibility.  An expired image
lease is quarantined: a timeout is not evidence that generation did not happen.
"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time


ROLES = {'image', 'reviewer', 'planner'}
CLAIM_ROLES = ROLES | {'flex'}
STATUSES = {'ready', 'leased', 'submitted', 'done', 'error', 'uncertain', 'cancelled', 'retry_wait'}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be a non-empty string.')
    return value


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 604800:
        raise ValueError('lease_seconds must be positive and at most seven days.')
    return float(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class WorkQueue:
    def __init__(self, run):
        self.run = Path(run).resolve()
        if not self.run.is_dir():
            raise ValueError('Run directory does not exist.')
        self.path = self.run / 'queue.sqlite3'
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    card TEXT,
                    task_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    worker TEXT,
                    token TEXT,
                    expires_at REAL,
                    claimed_at REAL,
                    registered_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_attempt INTEGER NOT NULL DEFAULT 0,
                    response TEXT,
                    response_sha256 TEXT,
                    error TEXT,
                    model TEXT,
                    cancel_reason TEXT,
                    uncertain_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_status_role ON jobs(status,role);
                CREATE TABLE IF NOT EXISTS affinity (
                    card TEXT PRIMARY KEY, worker TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_authors (
                    card TEXT NOT NULL, worker TEXT NOT NULL,
                    PRIMARY KEY(card,worker)
                );
                CREATE TABLE IF NOT EXISTS queue_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL NOT NULL, event TEXT NOT NULL,
                    job TEXT, detail TEXT NOT NULL
                );
            ''')
            # Runs created by the first queue version remain resumable.  Serialize
            # the additive migration, including when workers initialize together.
            db.execute('BEGIN IMMEDIATE')
            try:
                if 'uncertain_reason' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                    db.execute('ALTER TABLE jobs ADD COLUMN uncertain_reason TEXT')
                if 'priority' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                    db.execute('ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
                db.execute("INSERT OR IGNORE INTO image_authors SELECT DISTINCT card,worker FROM jobs WHERE role='image' AND card IS NOT NULL AND worker IS NOT NULL")
                db.commit()
            except BaseException:
                db.rollback()
                raise
            db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES ('artists','2')")
            db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES ('managed_retries','0')")
            for name,value in [('reviewers','1'),('workers','0'),('flexible','0')]:
                db.execute('INSERT OR IGNORE INTO settings(name,value) VALUES (?,?)',(name,value))

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _record(row):
        if row is None:
            return None
        result = dict(row)
        if result['response'] is not None:
            result['response'] = json.loads(result['response'])
        return result

    @staticmethod
    def _get(db, job):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('Unknown queue job: ' + str(job))
        return row

    @staticmethod
    def _event(db, event, job=None, **detail):
        db.execute('INSERT INTO queue_events(time,event,job,detail) VALUES (?,?,?,?)',
                   (time.time(), event, job, _json(detail)))

    def _expire(self, db, timestamp):
        managed = db.execute("SELECT value FROM settings WHERE name='managed_retries'").fetchone()[0] == '1'
        expired = db.execute("SELECT * FROM jobs WHERE status='leased' AND expires_at<=?", (timestamp,)).fetchall()
        for row in expired:
            if row['role'] == 'image':
                db.execute("UPDATE jobs SET status='uncertain',uncertain_reason=?,updated_at=? WHERE id=?",
                           ('Lease expired; generation outcome is unknown.', timestamp, row['id']))
                self._event(db, 'image_lease_uncertain', row['id'], worker=row['worker'],
                            reason='Lease expired; do not generate again without reconciling the original attempt.')
            else:
                status = 'retry_wait' if managed else 'ready'
                db.execute("UPDATE jobs SET status=?,worker=NULL,token=NULL,expires_at=NULL,updated_at=? WHERE id=?",
                           (status, timestamp, row['id']))
                self._event(db, 'lease_expired_retry_wait' if managed else 'lease_expired', row['id'], worker=row['worker'])

    def configure(self, artists=2, managed_retries=None, *, reviewers=None, workers=None):
        if isinstance(artists, bool) or not isinstance(artists, int) or artists < 1:
            raise ValueError('artists must be a positive integer.')
        if managed_retries is not None and not isinstance(managed_retries, bool):
            raise ValueError('managed_retries must be a boolean when supplied.')
        for name,value in [('reviewers',reviewers),('workers',workers)]:
            if value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<1):
                raise ValueError(name+' must be a positive integer.')
        with self._transaction() as db:
            db.execute("INSERT INTO settings(name,value) VALUES ('artists',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (str(artists),))
            if managed_retries is not None:
                db.execute("UPDATE settings SET value=? WHERE name='managed_retries'", ('1' if managed_retries else '0',))
            if reviewers is not None or workers is not None:
                was_flexible=db.execute("SELECT value FROM settings WHERE name='flexible'").fetchone()[0]=='1'
                db.execute("UPDATE settings SET value='1' WHERE name='flexible'")
                if reviewers is not None:db.execute("UPDATE settings SET value=? WHERE name='reviewers'",(str(reviewers),))
                if workers is not None or not was_flexible:
                    db.execute("UPDATE settings SET value=? WHERE name='workers'",(str(workers or artists+1),))
            managed = db.execute("SELECT value FROM settings WHERE name='managed_retries'").fetchone()[0] == '1'
            limits=dict(db.execute('SELECT name,value FROM settings').fetchall())
            self._event(db, 'configured', artists=artists, reviewers=int(limits['reviewers']), workers=int(limits['workers']),managed_retries=managed)
        return {'artists': artists, 'reviewers':int(limits['reviewers']), 'workers':int(limits['workers']) or None,'planners': 1, 'managed_retries': managed}

    def configuration(self):
        """Read the committed worker limits without changing leases or events."""
        with self._connection() as db:
            values = dict(db.execute('SELECT name,value FROM settings').fetchall())
        result={'artists': int(values['artists']), 'managed_retries': values['managed_retries'] == '1'}
        if values['flexible']=='1':result.update(reviewers=int(values['reviewers']),workers=int(values['workers']))
        return result

    def register(self, pending, card=None, kind=None, priority=0):
        if isinstance(priority,bool) or not isinstance(priority,int):raise ValueError('priority must be an integer.')
        identity = {key: _text(pending.get(key), key) for key in ('id', 'role', 'task_key', 'request_sha256')}
        if identity['role'] not in ROLES:
            raise ValueError('Unknown worker role.')
        kind = _text(kind or identity['id'].partition('-')[2], 'kind')
        if card is not None:
            _text(card, 'card')
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM jobs WHERE id=?', (identity['id'],)).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in identity.items()) or existing['card'] != card or existing['kind'] != kind:
                    raise ValueError('Queue job identity differs from its registered immutable request.')
                return self._record(existing)
            timestamp = time.time()
            db.execute('''INSERT INTO jobs(id,role,kind,card,task_key,request_sha256,registered_at,updated_at,priority)
                          VALUES (?,?,?,?,?,?,?,?,?)''',
                       (identity['id'], identity['role'], kind, card, identity['task_key'], identity['request_sha256'], timestamp, timestamp,priority))
            self._event(db, 'registered', identity['id'], role=identity['role'], kind=kind, card=card)
            return self._record(self._get(db, identity['id']))

    def set_priority(self,job,priority):
        if isinstance(priority,bool) or not isinstance(priority,int):raise ValueError('priority must be an integer.')
        with self._transaction() as db:
            row=self._get(db,job)
            if row['status']=='ready' and row['priority']!=priority:
                db.execute('UPDATE jobs SET priority=? WHERE id=?',(priority,job))
                self._event(db,'prioritized',job,priority=priority)
            return self._record(self._get(db,job))

    def jobs(self):
        with self._transaction() as db:
            self._expire(db, time.time())
            return [self._record(row) for row in db.execute('SELECT * FROM jobs ORDER BY sequence')]

    def claim(self, worker, role, lease_seconds=900):
        _text(worker, 'worker')
        if role not in CLAIM_ROLES:
            raise ValueError('Unknown worker role.')
        duration = _duration(lease_seconds)
        with self._transaction() as db:
            timestamp = time.time()
            self._expire(db, timestamp)
            # A physical executor owns one active job across ALL roles. Changing
            # roles must not double-book a process or bypass an uncertain call.
            active = ('leased', 'submitted', 'uncertain')
            if db.execute("SELECT 1 FROM jobs WHERE worker=? AND status IN (?,?,?) LIMIT 1",
                          (worker, *active)).fetchone():
                return None
            settings=dict(db.execute('SELECT name,value FROM settings').fetchall())
            flexible=settings['flexible']=='1'
            if role=='flex' and not flexible:raise ValueError('Flex workers require a version-two queue configuration.')
            total=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?)",active).fetchone()[0]
            if int(settings['workers']) and total>=int(settings['workers']):return None
            def candidates(actual_role):
                limit=int(settings['artists']) if actual_role=='image' else int(settings['reviewers']) if actual_role=='reviewer' else 1
                count=db.execute("SELECT COUNT(*) FROM jobs WHERE role=? AND status IN (?,?,?)",(actual_role,*active)).fetchone()[0]
                if count>=limit:return []
                rows=db.execute('''SELECT jobs.*,affinity.worker AS preferred_worker FROM jobs LEFT JOIN affinity ON affinity.card=jobs.card
                                   WHERE jobs.status='ready' AND jobs.role=?
                                   ORDER BY jobs.priority DESC,CASE WHEN affinity.worker=? THEN 0 ELSE 1 END,jobs.sequence''',(actual_role,worker)).fetchall()
                eligible=[]
                for item in rows:
                    if actual_role=='reviewer':
                        if item['kind'] in ('card_review','entity_review'):
                            if db.execute('SELECT 1 FROM image_authors WHERE card=? AND worker=?',(item['card'],worker)).fetchone():continue
                        elif role=='flex' or (flexible and db.execute('SELECT 1 FROM image_authors WHERE worker=?',(worker,)).fetchone()):continue
                    eligible.append(item)
                return eligible
            if role=='flex':
                reviews=candidates('reviewer');images=candidates('image')
                choices=(reviews or images) if len(reviews)>=2 or (reviews and reviews[0]['priority']>=400) else (images or reviews)
            else:choices=candidates(role)
            row=choices[0] if choices else None
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            db.execute("UPDATE jobs SET status='leased',worker=?,token=?,expires_at=?,claimed_at=?,updated_at=?,lease_attempt=lease_attempt+1 WHERE id=?",
                       (worker, token, timestamp + duration, timestamp, timestamp, row['id']))
            if row['role'] == 'image' and row['card'] is not None:
                db.execute('INSERT INTO affinity(card,worker) VALUES (?,?) ON CONFLICT(card) DO UPDATE SET worker=excluded.worker',
                           (row['card'], worker))
                db.execute('INSERT OR IGNORE INTO image_authors(card,worker) VALUES (?,?)',(row['card'],worker))
            self._event(db, 'claimed', row['id'], worker=worker, expires_at=timestamp + duration)
            return self._record(self._get(db, row['id']))

    @staticmethod
    def _owner_error(row, worker, token, allow_uncertain=False):
        if row['worker'] != worker or not token or row['token'] != token:
            return 'Lease owner or fencing token does not match.'
        allowed = ('leased', 'uncertain') if allow_uncertain and row['role'] == 'image' else ('leased',)
        if row['status'] not in allowed:
            return 'Job has no current executable lease: ' + row['status']
        return None

    def heartbeat(self, job, worker, token, lease_seconds=900):
        duration = _duration(lease_seconds)
        error = None
        with self._transaction() as db:
            timestamp = time.time()
            self._expire(db, timestamp)
            row = self._get(db, job)
            error = self._owner_error(row, worker, token)
            if not error:
                db.execute('UPDATE jobs SET expires_at=?,updated_at=? WHERE id=?', (timestamp + duration, timestamp, job))
                self._event(db, 'heartbeat', job, worker=worker, expires_at=timestamp + duration)
            result = self._record(self._get(db, job))
        # Expiration must stay committed even when the old holder is rejected.
        if error:
            raise ValueError(error)
        return result

    def deliver(self, job, worker, token, response, model='unspecified'):
        if not isinstance(response, dict):
            raise ValueError('Worker response must be a JSON object.')
        payload = _json(response)
        _text(model, 'model')
        error = None
        with self._transaction() as db:
            self._expire(db, time.time())
            row = self._get(db, job)
            same_owner = row['worker'] == worker and token and row['token'] == token
            if same_owner and row['status'] in ('submitted', 'done', 'error'):
                if row['response'] == payload and row['model'] == model and row['error'] is None:
                    return self._record(row)
                error = 'A different result was already delivered for this job.'
            else:
                error = self._owner_error(row, worker, token, allow_uncertain=True)
                if not error:
                    late = row['status'] == 'uncertain'
                    db.execute("UPDATE jobs SET status='submitted',response=?,response_sha256=?,error=NULL,model=?,updated_at=? WHERE id=?",
                               (payload, hashlib.sha256(payload.encode('utf-8')).hexdigest(), model, time.time(), job))
                    self._event(db, 'late_image_delivered' if late else 'delivered', job, worker=worker, model=model)
            result = self._record(self._get(db, job))
        if error:
            raise ValueError(error)
        return result

    def abandon(self, job, worker, token, reason):
        _text(reason, 'reason')
        error = None
        with self._transaction() as db:
            self._expire(db, time.time())
            row = self._get(db, job)
            if row['worker'] == worker and token and row['token'] == token and row['status'] in ('submitted', 'error') and row['error'] == reason and row['response'] is None:
                return self._record(row)
            error = self._owner_error(row, worker, token, allow_uncertain=True)
            if not error:
                db.execute("UPDATE jobs SET status='submitted',error=?,response=NULL,response_sha256=NULL,updated_at=? WHERE id=?", (reason, time.time(), job))
                self._event(db, 'abandoned', job, worker=worker, reason=reason)
            result = self._record(self._get(db, job))
        if error:
            raise ValueError(error)
        return result

    def quarantine(self, job, worker, token, reason):
        """Hold an ambiguous image attempt without scheduling another generation."""
        _text(reason, 'reason')
        error = None
        with self._transaction() as db:
            self._expire(db, time.time())
            row = self._get(db, job)
            if row['role'] != 'image':
                error = 'Only an image generation can be quarantined as an uncertain external attempt.'
            else:
                error = self._owner_error(row, worker, token, allow_uncertain=True)
            if not error:
                db.execute("UPDATE jobs SET status='uncertain',uncertain_reason=?,updated_at=? WHERE id=?",
                           (reason, time.time(), job))
                self._event(db, 'image_quarantined', job, worker=worker, reason=reason)
            result = self._record(self._get(db, job))
        if error:
            raise ValueError(error)
        return result

    def inbox(self):
        return [job for job in self.jobs() if job['status'] == 'submitted']

    def acknowledge(self, job, status='done'):
        if status not in ('done', 'error'):
            raise ValueError('Coordinator acknowledgement must be done or error.')
        with self._transaction() as db:
            row = self._get(db, job)
            if row['status'] == status:
                return self._record(row)
            if row['status'] != 'submitted':
                raise ValueError('Only an inbox result can be acknowledged.')
            db.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=?', (status, time.time(), job))
            self._event(db, 'acknowledged', job, status=status)
            return self._record(self._get(db, job))

    def cancel(self, job, reason):
        _text(reason, 'reason')
        with self._transaction() as db:
            row = self._get(db, job)
            if row['status'] in ('done', 'error', 'cancelled'):
                return self._record(row)
            if row['role'] == 'image' and row['status'] in ('leased', 'uncertain'):
                # Logical invalidation does not cancel an external image call.
                # Keep its reservation and original fencing token until pixels
                # arrive or an operator confirms it is no longer running.
                db.execute("UPDATE jobs SET status='uncertain',cancel_reason=?,uncertain_reason=?,updated_at=? WHERE id=?",
                           (reason, 'Cancellation requested; external generation may still be running.', time.time(), job))
                self._event(db, 'image_cancellation_held', job, reason=reason, previous_status=row['status'])
                return self._record(self._get(db, job))
            db.execute("UPDATE jobs SET status='cancelled',token=NULL,expires_at=NULL,cancel_reason=?,updated_at=? WHERE id=?",
                       (reason, time.time(), job))
            self._event(db, 'cancelled', job, reason=reason, previous_status=row['status'])
            return self._record(self._get(db, job))

    def settle_uncertain(self, job, reason):
        _text(reason, 'reason')
        with self._transaction() as db:
            row = self._get(db, job)
            if row['status'] != 'uncertain' or row['role'] != 'image':
                raise ValueError('Only an uncertain image attempt can be reconciled as not running and without a result.')
            error = 'Operator confirmed no result and no running generation: ' + reason
            db.execute("UPDATE jobs SET status='submitted',error=?,response=NULL,response_sha256=NULL,updated_at=? WHERE id=?", (error, time.time(), job))
            self._event(db, 'uncertain_settled', job, reason=reason)
            return self._record(self._get(db, job))

    def backup(self, path):
        """Create a consistent standalone SQLite snapshot, including WAL writes."""
        destination = Path(path).resolve()
        protected = {self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm')}
        if destination in protected:
            raise ValueError('Backup destination must differ from the live queue and its journal files.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as source:
            target = sqlite3.connect(destination, timeout=30)
            try:
                source.backup(target, pages=256, sleep=0.05)
                # A packed snapshot must not depend on a separately copied WAL.
                target.execute('PRAGMA journal_mode=DELETE')
                target.commit()
            finally:
                target.close()
        return destination
