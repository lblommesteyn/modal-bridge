"""One durable scheduler per database. Run: python -m modal_bridge.runner.

Settles jobs without a client polling, delivers completion callbacks, applies
retention, and dispatches batch runs."""
import fcntl
import logging
import os
import signal
import time
from .app import from_env


def main():
    app = from_env()
    runs = app.state.runs
    lock = open(runs.store.path + '.runner.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another scheduler owns this database')
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runs.recover()
    logging.basicConfig(level=logging.INFO)
    logging.info('Run scheduler started; global in-flight cap: 32')
    while not stopping:
        for tick in (app.state.reconciler.tick, runs.tick):
            try:
                tick()
            except Exception:
                logging.exception('Scheduler tick failed; will retry polling without resubmitting uncertain work')
        time.sleep(1)
    lock.close()


if __name__ == '__main__':
    main()
