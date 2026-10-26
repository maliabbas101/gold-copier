"""Offline end-to-end example. Uses no Discord token, account, or market data."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .agent import Journal, execute
from .signals import parse
from .store import Store


class SimulatedBroker:
    def __init__(self, follower):
        self.follower = follower

    def prepare(self, signal, job_id):
        return {'symbol': 'XAUUSD', 'side': signal['side'], 'comment': job_id}

    def send(self, request):
        return {'state': 'executed', 'detail': 'SIMULATED fill for ' + self.follower, 'ticket': 1}


def main():
    with TemporaryDirectory() as directory:
        base = Path(directory)
        store = Store(base / 'queue.db')
        store.pause(False)
        signal = parse('XAUUSD BUY\nENTRY: 2700\nSL: 2695\nTP: 2710\nBE: 2705')
        followers = ['alice-demo', 'bob-demo']
        store.ingest('discord-message-example', signal, time.time(), followers)
        for follower in followers:
            job = store.claim(follower)
            result = execute(job, SimulatedBroker(follower), Journal(base / f'{follower}.db'))
            store.finish(follower, job['id'], result)
            assert store.claim(follower) is None
        print(json.dumps(store.status(), indent=2))
        print('SIMULATION ONLY: copied to two mock accounts; duplicate claims returned no job.')


if __name__ == '__main__':
    main()
