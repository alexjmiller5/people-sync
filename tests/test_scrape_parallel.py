import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from people_sync.scrape import pace, run
from people_sync.scrape.profile import Profile


@pytest.mark.parametrize("workers", [4, 6, 10])
def test_tabs_share_queue_budget_and_serialize_writes(tmp_path, mocker, workers):
    records = [{"id": f"instagram:u{i}", "handle": f"u{i}"} for i in range(workers * 3)]
    mocker.patch.object(run, "_select_records", return_value=records)
    mocker.patch.object(pace.random, "uniform", return_value=0)
    mocker.patch.dict("os.environ", {pace.DAILY_CAPS_ENV: json.dumps({"instagram": workers * 2})})
    barrier = threading.Barrier(workers)
    seen = []
    targets = []
    writing = threading.Lock()

    class Browser:
        def watch_blocks(self, url, callback, stop):
            self.stop = stop

        def close(self):
            pass

    def connect(**kwargs):
        targets.append(kwargs["target_id"])
        return Browser()

    def collect(browser, module, platform, index, record):
        seen.append(record["id"])
        barrier.wait(timeout=3)
        return Profile(record_id=record["id"], platform=platform), "profiles/test/archive.json"

    def store(*args):
        assert writing.acquire(blocking=False), "concurrent writes to life-data"
        writing.release()

    mocker.patch.object(run.Browser, "connect", side_effect=connect)
    mocker.patch.object(run, "_collect_profile", side_effect=collect)
    mocker.patch.object(run, "_store_profile", side_effect=store)
    result = run.scrape(
        "instagram",
        targets=[f"tab-{i}" for i in range(workers)],
        state_path=str(tmp_path / "state.json"),
    )
    assert result == {"done": workers * 2, "skipped": 0, "halted": "daily cap reached"}
    assert set(targets) == {f"tab-{i}" for i in range(workers)}
    assert len(seen) == len(set(seen)) == workers * 2
    state = json.loads((tmp_path / "state.json").read_text())["instagram"]
    assert next(iter(state.values()))["attempts"] == workers * 2


@pytest.mark.parametrize("workers", [4, 6, 10])
def test_one_block_cancels_other_tabs_and_keeps_queue_pending(tmp_path, mocker, workers):
    records = [{"id": f"instagram:u{i}", "handle": f"u{i}"} for i in range(20)]
    mocker.patch.object(run, "_select_records", return_value=records)
    mocker.patch.object(pace.random, "uniform", return_value=0)
    barrier = threading.Barrier(workers)
    seen = []
    closed = []

    class Browser:
        def watch_blocks(self, url, callback, stop):
            self.halt, self.stop = callback, stop

        def close(self):
            closed.append(self)

    def collect(browser, module, platform, index, record):
        seen.append(record["id"])
        barrier.wait(timeout=3)
        if index == 0:
            browser.halt("HTTP 429")
        assert browser.stop.wait(3)
        raise run.ScrapeStopped()

    mocker.patch.object(run.Browser, "connect", side_effect=lambda **kw: Browser())
    mocker.patch.object(run, "_collect_profile", side_effect=collect)
    store = mocker.patch.object(run, "_store_profile")
    result = run.scrape(
        "instagram",
        targets=[f"tab-{i}" for i in range(workers)],
        state_path=str(tmp_path / "state.json"),
    )
    assert result == {"done": 0, "skipped": 0, "halted": "HTTP 429"}
    assert len(seen) == len(closed) == workers
    store.assert_not_called()


def test_attempt_reservation_is_atomic_and_preserves_prior_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv(pace.DAILY_CAPS_ENV, '{"instagram": 250}')
    p = pace.Pacer("instagram", str(tmp_path / "state.json"))
    (tmp_path / "state.json").write_text(
        json.dumps({"instagram": {p._today(): {"calls": 228, "gap_calls": 246}}})
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: p.reserve(), range(12)))
    assert results.count(True) == 4
    assert not p.allow()


@pytest.mark.parametrize("targets", [["a", "a"], [""], [f"tab-{i}" for i in range(11)]])
def test_invalid_targets_fail_before_querying_or_connecting(targets, mocker):
    query = mocker.patch.object(run, "_select_records")
    connect = mocker.patch.object(run.Browser, "connect")
    with pytest.raises(ValueError):
        run.scrape("instagram", targets=targets)
    query.assert_not_called()
    connect.assert_not_called()


def test_second_run_cannot_select_same_records(tmp_path, mocker):
    import fcntl

    state = str(tmp_path / "state.json")
    query = mocker.patch.object(run, "_select_records")
    with open(state + ".instagram.run.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run.scrape("instagram", targets=["a"], state_path=state)
    assert result["halted"] == "platform run already active"
    query.assert_not_called()


@pytest.mark.parametrize("workers", [4, 6, 10])
def test_tabs_divide_only_gap_not_the_global_break(tmp_path, mocker, workers):
    p = pace.Pacer("instagram", str(tmp_path / "state.json"))
    mocker.patch.object(pace.random, "uniform", return_value=8)
    for _ in range(24):
        assert p.next_gap(workers=workers) == 8 / workers
    mocker.patch.object(pace.random, "uniform", side_effect=[8, 200])
    assert p.next_gap(workers=workers) == 200 + 8 / workers
