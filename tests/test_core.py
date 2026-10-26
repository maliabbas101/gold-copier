import concurrent.futures
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from goldcopier.agent import Journal, execute
from goldcopier.risk import size_volume, validate_price
from goldcopier.server import create_app
from goldcopier.signals import parse
from goldcopier.store import Store

TEXT = 'XAUUSD BUY\nENTRY: 2700\nSL: 2695\nTP: 2710\nBE: 2705'


@pytest.mark.parametrize('text', [TEXT, 'XAUUSD SELL\nENTRY: 2700\nSL: 2710\nTP: 2680\nBE: 2690'])
def test_valid(text):
    assert parse(text).entry == 2700


@pytest.mark.parametrize('text', [TEXT.replace('XAUUSD', 'EURUSD'), TEXT.replace('2700', '2700-2702'),
    TEXT.replace('2695', '2701'), TEXT.replace('2705', '2715'), TEXT.replace('TP:', 'TP1:'),
    TEXT + '\nTP2: 2720', TEXT.replace('2700', 'NaN'), TEXT.replace('BE: 2705', 'SL: 2690')])
def test_reject_ambiguous(text):
    with pytest.raises(ValueError):
        parse(text)


def test_sizing_never_rounds_up():
    assert size_volume(10000, .5, 300, .01, .01, 100, 1) == .16
    with pytest.raises(ValueError):
        size_volume(100, .5, 1000, .01, .01, 100, 1)
    with pytest.raises(ValueError):
        size_volume(10000, 2, 300, .01, .01, 100, 1)


def test_quote_guards():
    s = parse(TEXT).asdict()
    assert validate_price(s, 2700, 2700.2, .5, .5) == 2700.2
    for bid, ask in [(2700, 2701), (2701, 2701.2), (2705, 2705.1), (2700, float('nan'))]:
        with pytest.raises(ValueError):
            validate_price(s, bid, ask, .5, .5)


def ready_store(tmp_path):
    store = Store(tmp_path / 'central.db')
    store.pause(False)
    return store


def test_pause_duplicate_expiry_and_no_redelivery(tmp_path):
    store = ready_store(tmp_path)
    assert store.ingest('1', parse(TEXT), time.time(), ['a','b'])
    assert not store.ingest('1', parse(TEXT), time.time(), ['a','b'])
    assert not store.ingest('2', parse(TEXT), time.time(), ['a','b'])
    assert store.claim('a')
    assert store.claim('a') is None
    store.pause(True)
    assert store.claim('b') is None
    assert 'cancelled' in [x['state'] for x in store.status()['jobs']]
    store.pause(False)
    with pytest.raises(ValueError):
        store.ingest('old', parse(TEXT), time.time()-61, ['a'])


def test_atomic_claim(tmp_path):
    store = ready_store(tmp_path)
    store.ingest('1', parse(TEXT), time.time(), ['a'])
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        claims = list(pool.map(lambda _: store.claim('a'), range(8)))
    assert sum(x is not None for x in claims) == 1


def test_api_account_isolation(tmp_path, monkeypatch):
    import hashlib
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'accounts': {a: hashlib.sha256((a*32).encode()).hexdigest() for a in ('a','b')}}))
    monkeypatch.setenv('COPIER_ADMIN_TOKEN', 'z'*32)
    app = create_app(config, tmp_path / 'db')
    app.state.store.pause(False)
    app.state.store.ingest('1', parse(TEXT), time.time(), ['a'])
    with TestClient(app) as client:
        assert client.post('/agent/claim').status_code == 401
        a = {'Authorization': 'Bearer ' + 'a'*32}
        b = {'Authorization': 'Bearer ' + 'b'*32}
        job = client.post('/agent/claim', headers=a).json()
        assert client.post('/agent/claim', headers=b).json() is None
        result = {'state':'executed','detail':'ok','ticket':1}
        route = f"/agent/jobs/{job['id']}/result"
        assert client.post(route, headers=b, json=result).status_code == 409
        assert client.post(route, headers=a, json=result).status_code == 200
        assert client.post(route, headers=a, json=result).status_code == 200
        assert client.get('/admin/status', headers=a).status_code == 403


class Broker:
    sent = 0
    def prepare(self, signal, job_id):
        return {'comment':job_id}
    def send(self, request):
        self.sent += 1
        raise TimeoutError()


def test_uncertain_order_is_persisted_and_blocks_new_orders(tmp_path):
    journal = Journal(tmp_path / 'agent.db')
    broker = Broker()
    job = {'id':'job1','signal':parse(TEXT).asdict(),'expires':time.time()+60}
    assert execute(job, broker, journal)['state'] == 'uncertain'
    assert journal.unresolved()
    with pytest.raises(sqlite3.IntegrityError):
        execute(job, broker, journal)
    assert broker.sent == 1


def test_crash_recovery_and_ack_retry(tmp_path):
    journal = Journal(tmp_path / 'agent.db')
    journal.reserve({'id':'crashed'})
    journal.recover()
    assert journal.unresolved()
    result = json.loads(journal.db.execute('SELECT result FROM executions').fetchone()[0])
    assert result['state'] == 'uncertain'


def test_expired_worker_never_sends(tmp_path):
    broker = Broker()
    job = {'id':'expired','signal':parse(TEXT).asdict(),'expires':time.time()-1}
    assert execute(job, broker, Journal(tmp_path / 'agent.db'))['state'] == 'rejected'
    assert broker.sent == 0


def test_ack_retry_never_submits_order_again(tmp_path):
    journal = Journal(tmp_path / 'worker.db')
    journal.reserve({'id':'filled'})
    journal.finish('filled', {'state':'executed','detail':'confirmed','ticket':12})
    class OfflineClient:
        def post(self, *args, **kwargs):
            import httpx
            raise httpx.ConnectError('offline')
    import httpx
    with pytest.raises(httpx.ConnectError):
        journal.flush(OfflineClient())
    assert journal.db.execute('SELECT acknowledged FROM executions').fetchone()[0] == 0
    class OnlineClient:
        def post(self, *args, **kwargs):
            return httpx.Response(200, request=httpx.Request('POST','https://example.com'))
    journal.flush(OnlineClient())
    assert journal.db.execute('SELECT acknowledged FROM executions').fetchone()[0] == 1


def test_new_store_is_paused_and_expired_jobs_are_not_claimed(tmp_path):
    store = Store(tmp_path / 'paused.db')
    with pytest.raises(ValueError):
        store.ingest('1', parse(TEXT), time.time(), ['a'])
    store.pause(False)
    store.ingest('2', parse(TEXT), time.time(), ['a'])
    with store.db() as db:
        db.execute('UPDATE signals SET expires=?', (time.time()-1,))
    assert store.claim('a') is None
    assert store.status()['jobs'][0]['state'] == 'expired'
