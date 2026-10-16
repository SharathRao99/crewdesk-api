"""The worker can run on a thread inside the API for a one-process host."""

import time

from sourcer import worker


def test_background_worker_polls_and_stops(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "run_once", lambda worker_id, **kw: calls.append(worker_id) or False)
    stop = worker.start_in_background(poll_seconds=0.01)
    deadline = time.time() + 5
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    stop()
    seen = len(calls)
    assert seen >= 3 and calls[0].endswith(":thread")
    time.sleep(0.1)
    assert len(calls) == seen  # stopped: no more polling


def test_api_starts_the_worker_only_when_asked(monkeypatch, api):
    started = []
    monkeypatch.setattr(worker, "start_in_background", lambda *a, **k: started.append(1) or (lambda: started.append("stopped")))
    from fastapi.testclient import TestClient

    from sourcer.api.app import app

    monkeypatch.delenv("RUN_WORKER_IN_API", raising=False)
    with TestClient(app):
        pass
    assert started == []
    monkeypatch.setenv("RUN_WORKER_IN_API", "1")
    with TestClient(app):
        assert started == [1]
    assert started == [1, "stopped"]
