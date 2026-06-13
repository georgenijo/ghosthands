#!/usr/bin/env python3
"""Pure unit tests for the agent-isolation registry — no apps, no driver.

All hardware-free (a fake launch_fn / a fake driver.call). They prove the four
isolation contracts, each a real assertion rather than a tautology:

1. windowed routing: a fake launch_fn hands out incrementing (pid, window)
   pairs, so two agents acquiring the SAME windowed bundle land on DISTINCT
   (pid, window_id); owner_of attributes each pid; release drops the rows.
2. exclusive serialisation DRIVEN THROUGH acquire(): two agents each
   acquire(kind="exclusive") the SAME bundle and wrap their action bursts in
   reg.exclusive(bundle) (via the returned handle). The shared occupancy
   counter never exceeds 1; a third agent on a DIFFERENT bundle runs
   concurrently (its own lock) — proving the lock is per-bundle, not global.
3. re-acquire leak: acquiring the same (agent,bundle) twice must not orphan the
   first pid in _owners; after release both pids resolve to None.
4. shared-pid no-clobber: two stateless/exclusive agents on the same shared
   instance must NOT each claim sole _owners — owner_of stays None.
5. windowed launch polls list_windows: a second instance whose window surfaces
   a beat late must not raise; _default_launch polls and finds it.

Run: python3 tests/test_isolation.py
"""

import itertools
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import actions, driver, isolation  # noqa: E402
from ghosthands.actions import App  # noqa: E402


def make_fake_launch():
    """A launch_fn replacement: every call returns a fresh, distinct
    (pid, window_id) and records what it was asked to launch."""
    pids = itertools.count(1000)
    windows = itertools.count(7000)
    calls: list[tuple[str, object]] = []

    def launch_fn(bundle_id: str, urls):
        calls.append((bundle_id, urls))
        return next(pids), next(windows)

    return launch_fn, calls


def make_scripted_launch(pids):
    """A launch_fn that yields the given pids in order (one per call) with a
    matching window each, for re-acquire tests that pin specific pids."""
    it = iter(pids)
    win = itertools.count(7000)

    def launch_fn(bundle_id: str, urls):
        return next(it), next(win)

    return launch_fn


def test_windowed_gives_distinct_pid_window() -> None:
    launch_fn, calls = make_fake_launch()
    reg = isolation.Registry(launch_fn=launch_fn)

    a = reg.acquire("agent-A", "com.apple.TextEdit", kind="windowed")
    b = reg.acquire("agent-B", "com.apple.TextEdit", kind="windowed")

    assert isinstance(a, App) and isinstance(b, App), "acquire must return actions.App"
    assert (a.pid, a.window_id) != (b.pid, b.window_id), (
        f"agents collided on same window: {(a.pid, a.window_id)}")
    assert a.pid != b.pid and a.window_id != b.window_id, "pid AND window must differ"

    # each launch asked for a distinct new instance (one call per agent)
    assert len(calls) == 2, f"expected 2 launches, got {len(calls)}"
    assert all(bundle == "com.apple.TextEdit" for bundle, _ in calls)

    # ownership maps pid -> agent
    assert reg.owner_of(a.pid) == "agent-A", reg.owner_of(a.pid)
    assert reg.owner_of(b.pid) == "agent-B", reg.owner_of(b.pid)
    assert reg.owner_of(999999) is None, "unknown pid must be unattributed"


def test_release_drops_ownership() -> None:
    launch_fn, _ = make_fake_launch()
    reg = isolation.Registry(launch_fn=launch_fn)

    a = reg.acquire("agent-A", "com.apple.TextEdit", kind="windowed")
    assert reg.owner_of(a.pid) == "agent-A"

    reg.release("agent-A")
    assert reg.owner_of(a.pid) is None, "release must drop the ownership row"
    assert reg.leases_of("agent-A") == [], "release must drop the leases"
    reg.release("agent-A")  # idempotent — no raise


def test_unknown_kind_rejected() -> None:
    reg = isolation.Registry(launch_fn=make_fake_launch()[0])
    try:
        reg.acquire("agent-A", "com.apple.TextEdit", kind="bogus")
    except isolation.IsolationError:
        return
    raise AssertionError("unknown kind must raise IsolationError")


def test_reacquire_does_not_orphan_old_pid() -> None:
    """BUG 2: re-acquiring the same (agent, bundle) must evict the prior pid
    from _owners, not orphan it. After acquiring pid 1000 then pid 1001 for the
    same agent+bundle and releasing, owner_of(1000) must be None (not still the
    agent), and 1001 reflects only the live state — which release then clears."""
    bundle = "com.apple.TextEdit"
    reg = isolation.Registry(launch_fn=make_scripted_launch([1000, 1001]))

    a1 = reg.acquire("agent-A", bundle, kind="windowed")
    assert a1.pid == 1000 and reg.owner_of(1000) == "agent-A"

    a2 = reg.acquire("agent-A", bundle, kind="windowed")  # same key, new instance
    assert a2.pid == 1001
    # the overwrite must NOT leave 1000 attributed to agent-A
    assert reg.owner_of(1000) is None, "re-acquire orphaned the old pid in _owners"
    assert reg.owner_of(1001) == "agent-A", "live pid must be attributed"
    # only one lease survives for this key
    assert len(reg.leases_of("agent-A")) == 1, reg.leases_of("agent-A")

    reg.release("agent-A")
    assert reg.owner_of(1000) is None and reg.owner_of(1001) is None, (
        "release must leave nothing attributed")


def test_shared_pid_not_clobbered() -> None:
    """BUG 3: two agents on the SAME shared instance (stateless/exclusive) must
    not each claim sole _owners — that would let the 2nd clobber the 1st and
    make owner_of lie. owner_of stays None for a shared pid. We fake
    actions.App.launch so both agents land on the same (pid, window)."""
    bundle = "com.apple.TextEdit"
    shared = App(pid=4242, window_id=9001, name=bundle)
    orig_launch = actions.App.launch
    actions.App.launch = classmethod(lambda cls, b, *, urls=None: shared)
    try:
        reg = isolation.Registry()
        h_a = reg.acquire("agent-A", bundle, kind="exclusive")
        s_b = reg.acquire("agent-B", bundle, kind="stateless")

        assert isinstance(h_a, isolation.ExclusiveHandle), "exclusive returns a handle"
        assert h_a.app.pid == 4242 and s_b.pid == 4242, "both bound to the shared pid"
        # neither claimed sole ownership of the shared pid
        assert reg.owner_of(4242) is None, (
            "shared pid must stay unattributed (no clobber), got "
            f"{reg.owner_of(4242)!r}")
        # but both leases exist and are tracked per-agent
        assert len(reg.leases_of("agent-A")) == 1 and len(reg.leases_of("agent-B")) == 1
    finally:
        actions.App.launch = orig_launch


def test_acquire_exclusive_serialises_on_same_target() -> None:
    """BUG 1: acquire(kind="exclusive") + reg.exclusive(bundle) must mutually-
    exclude two agents on the SAME bundle. Driven entirely through acquire() —
    each agent gets a handle whose .exclusive() guards its action burst — so
    this proves the public exclusive path, not a bare lock. A third agent on a
    DIFFERENT bundle runs concurrently, proving the lock is per-bundle."""
    bundle = "com.apple.TextEdit"
    other = "com.apple.Calculator"
    shared = App(pid=4242, window_id=9001, name=bundle)
    orig_launch = actions.App.launch
    actions.App.launch = classmethod(lambda cls, b, *, urls=None: shared)
    try:
        reg = isolation.Registry()

        occupancy = {bundle: 0, other: 0}
        max_seen = {bundle: 0, other: 0}
        overlaps = 0
        ran_other_concurrently = False
        state_lock = threading.Lock()
        entered = {"A": 0, "B": 0, "C": 0}

        def worker(name: str, agent: str, b: str, rounds: int) -> None:
            nonlocal overlaps, ran_other_concurrently
            handle = reg.acquire(agent, b, kind="exclusive")
            for _ in range(rounds):
                with handle.exclusive():  # the enforcement half of acquire()
                    with state_lock:
                        occupancy[b] += 1
                        entered[name] += 1
                        if b == bundle and occupancy[bundle] > 1:
                            overlaps += 1
                        max_seen[b] = max(max_seen[b], occupancy[b])
                        # C (other bundle) overlapping A/B proves per-bundle lock
                        if b == other and (occupancy[bundle] > 0):
                            ran_other_concurrently = True
                    time.sleep(0.002)  # widen the window for a race to show
                    with state_lock:
                        occupancy[b] -= 1

        rounds = 50
        threads = [
            threading.Thread(target=worker, args=("A", "agent-A", bundle, rounds)),
            threading.Thread(target=worker, args=("B", "agent-B", bundle, rounds)),
            threading.Thread(target=worker, args=("C", "agent-C", other, rounds)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert overlaps == 0, f"exclusive section overlapped {overlaps} times on {bundle}"
        assert max_seen[bundle] == 1, (
            f"max concurrent occupancy on {bundle} was {max_seen[bundle]}, expected 1")
        assert entered["A"] == rounds and entered["B"] == rounds, "all A/B rounds must run"
        assert entered["C"] == rounds, "the other-bundle agent must finish too"
        assert ran_other_concurrently, (
            "different bundles must run concurrently — lock is not per-bundle")

        # both same-bundle contenders shared ONE lock; the other bundle differs
        assert reg.lock_for(bundle) is reg.lock_for(bundle), "lock stable per bundle"
        assert reg.lock_for(bundle) is not reg.lock_for(other), "per-bundle, not global"
    finally:
        actions.App.launch = orig_launch


def test_default_launch_polls_for_late_window() -> None:
    """BUG 4: _default_launch must poll list_windows for a 2nd instance whose
    window surfaces a beat after launch (like actions.App.launch), not raise on
    the first empty windows array. We fake driver.call: launch_app returns NO
    window, then list_windows returns one on the 2nd poll. Hermetic — no daemon."""
    bundle = "com.apple.TextEdit"
    state = {"launch": 0, "list": 0}

    def fake_call(tool, args=None, **kw):
        if tool == "launch_app":
            state["launch"] += 1
            return {"pid": 5555, "windows": []}  # window not up yet
        if tool == "list_windows":
            state["list"] += 1
            if state["list"] < 2:
                return {"windows": []}  # still empty on first poll
            return {"windows": [{"window_id": 8888, "is_on_screen": True, "title": "Untitled"}]}
        raise AssertionError(f"unexpected tool {tool}")

    orig_call = driver.call
    orig_secs = actions.WINDOW_POLL_SECONDS
    driver.call = fake_call
    actions.WINDOW_POLL_SECONDS = 0.001  # keep the test fast
    try:
        pid, window_id = isolation._default_launch(bundle, None)
        assert (pid, window_id) == (5555, 8888), (pid, window_id)
        assert state["launch"] == 1, "must launch exactly one new instance"
        assert state["list"] >= 2, "must have polled list_windows until the window appeared"
    finally:
        driver.call = orig_call
        actions.WINDOW_POLL_SECONDS = orig_secs


def test_default_launch_raises_when_no_window_ever() -> None:
    """BUG 4 boundary: if no window EVER surfaces after polling, _default_launch
    raises IsolationError (not e.g. a KeyError on result['windows'])."""
    bundle = "com.apple.TextEdit"

    def fake_call(tool, args=None, **kw):
        if tool == "launch_app":
            return {"pid": 6666, "windows": []}
        if tool == "list_windows":
            return {"windows": []}
        raise AssertionError(f"unexpected tool {tool}")

    orig_call = driver.call
    orig_secs = actions.WINDOW_POLL_SECONDS
    orig_attempts = actions.WINDOW_POLL_ATTEMPTS
    driver.call = fake_call
    actions.WINDOW_POLL_SECONDS = 0.001
    actions.WINDOW_POLL_ATTEMPTS = 3  # keep the test fast
    try:
        try:
            isolation._default_launch(bundle, None)
        except isolation.IsolationError:
            return
        raise AssertionError("must raise IsolationError when no window appears")
    finally:
        driver.call = orig_call
        actions.WINDOW_POLL_SECONDS = orig_secs
        actions.WINDOW_POLL_ATTEMPTS = orig_attempts


def main() -> int:
    tests = [
        test_windowed_gives_distinct_pid_window,
        test_release_drops_ownership,
        test_unknown_kind_rejected,
        test_reacquire_does_not_orphan_old_pid,
        test_shared_pid_not_clobbered,
        test_acquire_exclusive_serialises_on_same_target,
        test_default_launch_polls_for_late_window,
        test_default_launch_raises_when_no_window_ever,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nALL {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
