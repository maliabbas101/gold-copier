import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

import httpx

from .mt5broker import MT5Broker


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY, job TEXT NOT NULL, request TEXT,
            result TEXT, acknowledged INTEGER NOT NULL DEFAULT 0)''')
        self.db.commit()

    def reserve(self, job):
        self.db.execute('INSERT INTO executions(id,job) VALUES (?,?)',
                        (job['id'], json.dumps(job)))
        self.db.commit()

    def prepared(self, job_id, request):
        # Persist the intent before calling the broker. NULL result after restart is uncertain.
        self.db.execute('UPDATE executions SET request=? WHERE id=?',
                        (json.dumps(request), job_id))
        self.db.commit()

    def finish(self, job_id, result):
        self.db.execute('UPDATE executions SET result=? WHERE id=?', (json.dumps(result), job_id))
        self.db.commit()

    def unresolved(self):
        return self.db.execute("SELECT 1 FROM executions WHERE result IS NULL "
                               "OR json_extract(result,'$.state')='uncertain' LIMIT 1").fetchone() is not None

    def recover(self):
        for row in self.db.execute('SELECT id FROM executions WHERE result IS NULL').fetchall():
            self.finish(row['id'], {'state': 'uncertain',
                                   'detail': 'Worker stopped before confirmation; manual reconciliation required'})

    def flush(self, client):
        for row in self.db.execute('SELECT id,result FROM executions WHERE acknowledged=0 AND result IS NOT NULL').fetchall():
            response = client.post(f"/agent/jobs/{row['id']}/result", json=json.loads(row['result']))
            response.raise_for_status()
            self.db.execute('UPDATE executions SET acknowledged=1 WHERE id=?', (row['id'],))
            self.db.commit()

    def monitor(self, broker):
        for row in self.db.execute("SELECT job,request FROM executions WHERE request IS NOT NULL "
                                   "AND (result IS NULL OR json_extract(result,'$.state') IN ('executed','uncertain'))").fetchall():
            try:
                broker.breakeven(json.loads(row['request']), json.loads(row['job'])['signal']['be'])
            except Exception:
                logging.exception('BE monitoring failed; inspect terminal')


def execute(job, broker, journal):
    journal.reserve(job)
    try:
        if time.time() >= job['expires']:
            raise ValueError('Signal expired before execution')
        if Path('PAUSE').exists():
            raise ValueError('Local worker paused')
        request = broker.prepare(job['signal'], job['id'])
        if time.time() >= job['expires']:
            raise ValueError('Signal expired during broker preflight')
    except Exception:
        result = {'state': 'rejected', 'detail': 'Preflight rejected; inspect local worker log'}
        logging.exception('Preflight rejected')
        journal.finish(job['id'], result)
        return result
    journal.prepared(job['id'], request)
    try:
        result = broker.send(request)
    except Exception:
        logging.exception('Broker submission unconfirmed')
        result = {'state': 'uncertain', 'detail': 'Submission interrupted; reconcile terminal history'}
    journal.finish(job['id'], result)
    return result


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = json.loads(Path(os.environ.get('AGENT_CONFIG', 'agent.json')).read_text())
    url = os.environ['COPIER_URL'].rstrip('/')
    parsed = urlparse(url)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
        raise ValueError('HTTPS is required outside localhost')
    # Local exclusive lock prevents duplicate worker processes for one journal.
    lock = Path(cfg.get('journal', 'agent.db') + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
        journal = Journal(cfg.get('journal', 'agent.db'))
        journal.recover()
        broker = MT5Broker(cfg)
        with httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + os.environ['AGENT_TOKEN']},
                          timeout=15, follow_redirects=False) as client:
            while True:
                # BE continues locally even if service/Discord is offline or paused.
                journal.monitor(broker)
                try:
                    journal.flush(client)
                    if not Path('PAUSE').exists() and not journal.unresolved():
                        response = client.post('/agent/claim')
                        response.raise_for_status()
                        job = response.json()
                        if job is not None:
                            execute(job, broker, journal)
                            journal.flush(client)
                except (httpx.HTTPError, ValueError, sqlite3.Error):
                    logging.exception('Worker sync failed; no automatic trade resubmission')
                time.sleep(1)
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
