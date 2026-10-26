import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('paused', 'true');
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, account TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', result TEXT,
                    UNIQUE(signal_id, account));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pause(self, paused):
        with self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='paused'", (json.dumps(paused),))
            if paused:
                db.execute("UPDATE jobs SET state='cancelled' WHERE state='queued'")

    def ingest(self, source_id, signal, created, accounts, ttl=60):
        now = time.time()
        if created > now + 5 or now - created > ttl:
            raise ValueError('Stale or future signal')
        payload = json.dumps(signal.asdict(), sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == 'true':
                raise ValueError('Copier is paused')
            if db.execute('SELECT 1 FROM signals WHERE id=? OR (fingerprint=? AND expires>?)',
                          (source_id, fingerprint, now)).fetchone():
                return False
            db.execute('INSERT INTO signals VALUES (?,?,?,?,?)',
                       (source_id, fingerprint, payload, created, created + ttl))
            for account in accounts:
                job_id = hashlib.sha256(f'{source_id}:{account}'.encode()).hexdigest()[:24]
                db.execute('INSERT INTO jobs(id, signal_id, account) VALUES(?,?,?)',
                           (job_id, source_id, account))
        return True

    def claim(self, account):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == 'true':
                return None
            db.execute("UPDATE jobs SET state='expired' WHERE state='queued' AND signal_id IN "
                       '(SELECT id FROM signals WHERE expires<=?)', (time.time(),))
            row = db.execute("SELECT j.id,s.payload,s.expires FROM jobs j JOIN signals s "
                             "ON s.id=j.signal_id WHERE account=? AND state='queued' "
                             'ORDER BY s.created LIMIT 1', (account,)).fetchone()
            if not row:
                return None
            # Intentionally no lease retry: lost response -> manual reconciliation, never another fill.
            db.execute("UPDATE jobs SET state='claimed' WHERE id=?", (row['id'],))
            return {'id': row['id'], 'signal': json.loads(row['payload']), 'expires': row['expires']}

    def finish(self, account, job_id, result):
        if result.get('state') not in ('executed', 'rejected', 'uncertain'):
            raise ValueError('Invalid result state')
        with self.db() as db:
            row = db.execute('SELECT state,result FROM jobs WHERE id=? AND account=?',
                             (job_id, account)).fetchone()
            if row is None:
                raise ValueError('Unknown job')
            payload = json.dumps(result, sort_keys=True)
            if row['state'] == 'claimed':
                db.execute('UPDATE jobs SET state=?,result=? WHERE id=?',
                           (result['state'], payload, job_id))
            elif row['result'] != payload:
                raise ValueError('Result conflicts with previous acknowledgement')

    def status(self):
        with self.db() as db:
            return {'paused': json.loads(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]),
                    'jobs': [dict(x) for x in db.execute('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 100')]}
