"""
Regression test for the single-worker COM executor under a timeout.

``server._run`` offloads every COM call to a ``ThreadPoolExecutor`` pinned to
ONE worker thread (COM objects are apartment-threaded, so all calls must run
on the same OS thread -- see server.py, "Single-threaded COM executor").
``asyncio.wait_for`` can abandon the *asyncio* future when a call overruns
its budget, but it cannot force-cancel the underlying thread: a genuinely
stuck COM call (e.g. a modal dialog SolidWorks is waiting on) keeps running
on that one worker after _run has already raised a timeout for it. Every
call made afterwards queues behind it, and -- since it never gets a chance
to start before ITS OWN budget elapses -- used to time out too, reporting
the exact same generic message ("may be blocked by a dialog") about an
operation that was never actually attempted.

This is reproducible without SolidWorks: _run takes an arbitrary callable,
so a plain blocking function stands in for a stuck COM call. Both calls run
on ONE persistent background event loop for the whole test (started here,
stopped at teardown) rather than one ``asyncio.run()`` per call: the real
server keeps a single event loop alive for its whole process lifetime, and
_run's done-callback (which clears the "stuck" marker once the blocked
thread actually finishes) is scheduled back onto whichever loop created it
-- an ephemeral per-call loop that is already closed by the time the real
work finishes would silently lose that callback and make the test flaky
for reasons that have nothing to do with the bug being tested.

Run with:  python -m pytest tests/test_timeout_lockup.py -q
"""

import asyncio
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client",
                    reason="the server module needs pywin32 (Windows only)")

import server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_stuck_call():
    """Each test starts from a clean slate and leaves one behind it."""
    server._stuck_call = None
    yield
    server._stuck_call = None


@pytest.fixture
def short_timeout(monkeypatch):
    """Give every call a tiny budget so the test does not wait 15-120s."""
    monkeypatch.setattr(server, "_timeout_budget_for", lambda name: 0.1)


@pytest.fixture
def server_loop():
    """One event loop alive for the whole test, like the real server's."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _run_on(loop, coro, timeout=5):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _start_a_stuck_call(loop, release):
    """Drive a genuinely-blocked call through server._run and return once
    it has reported its own timeout (_stuck_call is set) -- mirroring
    exactly what a real stuck COM call looks like from _run's perspective."""
    started = threading.Event()

    def _blocked():
        started.set()
        release.wait(timeout=5)
        return "finished-late"

    result_box = {}

    def _driver():
        try:
            _run_on(loop, server._run(_blocked))
        except Exception as exc:
            result_box["error"] = exc
        else:
            result_box["error"] = None

    thread = threading.Thread(target=_driver, daemon=True)
    thread.start()
    assert started.wait(timeout=2), "worker thread never picked up the stuck call"
    thread.join(timeout=2)
    assert isinstance(result_box.get("error"), RuntimeError), (
        f"the stuck call's own _run did not time out as expected: {result_box}"
    )
    return thread


def test_a_timed_out_call_leaves_the_worker_thread_running_and_is_remembered(
    short_timeout, server_loop
):
    """The stuck call's own _run invocation must report the timeout (not
    hang forever) and must record that something is still running."""
    release = threading.Event()
    _start_a_stuck_call(server_loop, release)
    try:
        assert server._stuck_call is not None
        assert server._stuck_call["tool"] == "_blocked"
    finally:
        release.set()


def test_a_later_call_fails_fast_with_a_clear_reason_instead_of_its_own_timeout(
    short_timeout, server_loop
):
    """The bug: a fast, healthy call made right after the stuck one used to
    get the SAME generic timeout message after waiting out its own budget,
    despite never starting. It must now say plainly that it is blocked by
    the earlier stuck call, and must fail immediately."""
    release = threading.Event()
    _start_a_stuck_call(server_loop, release)
    try:
        started = threading.Event()

        def fast():
            started.set()
            return "fast-result"

        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="still blocked on an earlier call") as excinfo:
            _run_on(server_loop, server._run(fast))
        elapsed = time.monotonic() - t0

        assert not started.is_set(), "the new call must never have been submitted/run at all"
        assert "cannot be safely cancelled" in str(excinfo.value)
        assert elapsed < 0.1, (
            "must fail immediately -- it must not wait out another timeout window"
        )
    finally:
        release.set()


def test_the_stuck_marker_clears_once_the_thread_actually_finishes(
    short_timeout, server_loop
):
    """Once the real work completes, the executor is healthy again: a new
    call is not permanently poisoned by one that eventually returned."""
    release = threading.Event()
    _start_a_stuck_call(server_loop, release)
    assert server._stuck_call is not None

    release.set()

    deadline = time.monotonic() + 2
    while server._stuck_call is not None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert server._stuck_call is None, "stuck marker never cleared after the call finished"

    result = _run_on(server_loop, server._run(lambda: "ok"))
    assert result == "ok"
