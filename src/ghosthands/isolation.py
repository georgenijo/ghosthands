"""Keep concurrent agents from colliding on shared apps.

When several runs (subagents, parallel flows) drive macOS at once they share
one global desktop: the same app instance, the same focused window, the same
clipboard. This registry routes each agent — keyed by the who-sent-it tag it
declares — to its own lane, with three strategies of increasing strictness:

- ``stateless`` (route): no-op. The agent shares whatever window exists; use
  when actions are independent or the app is naturally single-target.
- ``windowed`` (isolate): each agent gets a DISTINCT app instance+window via
  ``launch_app{creates_new_application_instance:True}``, so two agents never
  touch the same ``(pid, window_id)``. The default.
- ``exclusive`` (lock+queue): all agents bind to the ONE shared instance, but a
  per-``bundle_id`` ``threading.Lock`` serialises their action bursts, so only
  one agent acts on the shared target at a time; the others queue.

The ``exclusive`` lane is two halves that MUST be used together: ``acquire``
records the lease and hands back a handle, but it provides no isolation on its
own — the App it returns drives the shared window. The mutual exclusion only
happens when each agent wraps its action burst in :meth:`exclusive` (or
``handle.exclusive()``), whose lock is keyed off the same ``bundle_id``. Two
exclusive agents on the same bundle therefore serialise; agents on different
bundles run concurrently (different locks).

The lock/registry bookkeeping is pure and hardware-free: launching is factored
behind an injectable ``launch_fn`` so it can be unit-tested with a fake that
hands back incrementing pids/windows. Only an actual ``acquire(kind="windowed")``
against the real ``driver.call`` touches hardware.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import actions, driver

# A launch_fn takes (bundle_id, urls) and returns (pid, window_id).
LaunchFn = Callable[[str, "list[str] | None"], "tuple[int, int]"]

WINDOWED = "windowed"
EXCLUSIVE = "exclusive"
STATELESS = "stateless"
_KINDS = frozenset({WINDOWED, EXCLUSIVE, STATELESS})


class IsolationError(RuntimeError):
    pass


def _default_launch(bundle_id: str, urls: list[str] | None) -> tuple[int, int]:
    """Launch a fresh, distinct instance via the driver and pick its window.

    Each call asks the daemon for a brand-new application instance so the
    returned ``(pid, window_id)`` is unique to the calling agent. A second
    instance often surfaces its window a beat after the launch call returns
    (the same lag :meth:`actions.App.launch` polls around), so we poll
    ``list_windows`` for the new pid — reusing ``actions.WINDOW_POLL_ATTEMPTS``
    / ``actions.WINDOW_POLL_SECONDS`` and ``actions.App._pick_window`` — before
    giving up, instead of failing on the first empty ``windows`` array."""
    args: dict = {"bundle_id": bundle_id, "creates_new_application_instance": True}
    if urls:
        args["urls"] = urls
    result = driver.call("launch_app", args)
    pid = result["pid"]

    window = actions.App._pick_window(result.get("windows", []))
    for _ in range(actions.WINDOW_POLL_ATTEMPTS):
        if window is not None:
            break
        time.sleep(actions.WINDOW_POLL_SECONDS)
        listed = driver.call("list_windows", {"pid": pid})
        windows = listed["windows"] if isinstance(listed, dict) else listed
        window = actions.App._pick_window(windows or [])
    if window is None:
        raise IsolationError(f"{bundle_id} (pid {pid}): no on-screen window appeared")
    return pid, window["window_id"]


@dataclass
class _Lease:
    """What one agent currently holds for one bundle."""

    agent: str
    bundle_id: str
    kind: str
    pid: int | None = None
    window_id: int | None = None


@dataclass
class ExclusiveHandle:
    """The result of ``acquire(kind="exclusive")``: a shared-instance ``App``
    bound to the registry's per-bundle lock.

    The ``app`` drives the ONE shared window — acquiring the handle does NOT
    isolate anything by itself. Serialisation happens only while the caller
    holds the lock, which is what :meth:`exclusive` does::

        h = reg.acquire("agent-A", bundle, kind="exclusive")
        with h.exclusive():          # or: with reg.exclusive(bundle):
            h.app.click("Save")      # no other exclusive agent can act now

    The lock is keyed off ``bundle_id``, so every exclusive agent on the same
    bundle shares it and they mutually-exclude; different bundles, different
    locks, run concurrently.
    """

    app: actions.App
    bundle_id: str
    _registry: "Registry"

    @contextmanager
    def exclusive(self) -> Iterator[actions.App]:
        """Hold this bundle's lock for the body and yield the shared ``app``."""
        with self._registry.exclusive(self.bundle_id):
            yield self.app


@dataclass
class Registry:
    """Routes agents to non-colliding lanes, keyed by the who-sent-it tag.

    Thread-safe: the registry mutations are guarded by an internal lock, and
    ``exclusive``/``acquire(kind="exclusive")`` hand out a per-``bundle_id``
    lock that serialises the agents contending for one shared target.
    """

    launch_fn: LaunchFn = _default_launch
    _leases: dict[tuple[str, str], _Lease] = field(default_factory=dict, init=False, repr=False)
    _owners: dict[int, str] = field(default_factory=dict, init=False, repr=False)
    _locks: dict[str, threading.Lock] = field(default_factory=dict, init=False, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # -- acquisition ---------------------------------------------------------

    def acquire(self, agent: str, bundle_id: str, kind: str = WINDOWED, *,
                urls: list[str] | None = None) -> actions.App | ExclusiveHandle:
        """Hand ``agent`` a target to drive ``bundle_id`` under ``kind``.

        ``windowed`` launches a DISTINCT instance and records the
        agent->(pid, window) ownership so :meth:`owner_of` can attribute later
        actions to a single agent; it returns an :class:`actions.App`.

        ``stateless`` and ``exclusive`` bind to the app's current on-screen
        window (no new instance) — a SHARED pid that other agents also touch.
        We therefore do NOT claim sole ``_owners`` for a shared pid (that would
        let a later agent clobber an earlier one, and ``owner_of`` would lie).
        ``stateless`` returns the shared ``App`` directly; ``exclusive`` returns
        an :class:`ExclusiveHandle` whose :meth:`ExclusiveHandle.exclusive`
        (equivalently :meth:`exclusive`) provides the actual serialisation.

        Re-acquiring the same ``(agent, bundle_id)`` replaces the lease; the
        old lease's pid is dropped from ``_owners`` first so a stale pid is
        never left attributed to the agent.
        """
        if kind not in _KINDS:
            raise IsolationError(f"unknown isolation kind {kind!r}; expected one of {sorted(_KINDS)}")

        if kind == WINDOWED:
            pid, window_id = self.launch_fn(bundle_id, urls)
            with self._guard:
                self._evict_old_lease(agent, bundle_id)
                self._leases[(agent, bundle_id)] = _Lease(agent, bundle_id, kind, pid, window_id)
                # Distinct-instance pid: this agent solely owns it.
                self._owners[pid] = agent
            return actions.App(pid, window_id, name=bundle_id)

        # stateless + exclusive both attach to the existing (shared) window.
        app = actions.App.launch(bundle_id, urls=urls)
        with self._guard:
            self._evict_old_lease(agent, bundle_id)
            self._leases[(agent, bundle_id)] = _Lease(agent, bundle_id, kind, app.pid, app.window_id)
            # Shared pid: do NOT claim _owners — many agents touch this pid, so
            # sole ownership would be a lie and a later agent would clobber an
            # earlier one's row. owner_of stays None for shared pids.
        if kind == EXCLUSIVE:
            return ExclusiveHandle(app=app, bundle_id=bundle_id, _registry=self)
        return app

    def _evict_old_lease(self, agent: str, bundle_id: str) -> None:
        """Drop the prior lease for ``(agent, bundle_id)`` and its owner row.
        Must be called holding ``self._guard``. Without this, re-acquiring the
        same key overwrites ``_leases`` but orphans the previous pid in
        ``_owners`` forever — ``owner_of(old_pid)`` would keep returning the
        agent even after :meth:`release`."""
        prev = self._leases.get((agent, bundle_id))
        if prev is not None and prev.pid is not None and self._owners.get(prev.pid) == agent:
            del self._owners[prev.pid]

    # -- ownership / release -------------------------------------------------

    def owner_of(self, pid: int) -> str | None:
        """Which agent SOLELY owns the run on ``pid`` (None if unattributed).

        Contract: only ``windowed`` (distinct-instance) pids are attributed —
        each is owned by exactly one agent. ``stateless`` and ``exclusive``
        bind to a SHARED instance whose pid many agents touch, so it is left
        unattributed (returns None) rather than pinned to whichever agent
        happened to acquire last."""
        with self._guard:
            return self._owners.get(pid)

    def leases_of(self, agent: str) -> list[_Lease]:
        with self._guard:
            return [lease for lease in self._leases.values() if lease.agent == agent]

    def release(self, agent: str) -> None:
        """Drop everything ``agent`` holds (its ownership rows and leases).
        Idempotent — releasing an unknown agent is a no-op."""
        with self._guard:
            keys = [key for key, lease in self._leases.items() if lease.agent == agent]
            for key in keys:
                lease = self._leases.pop(key)
                if lease.pid is not None and self._owners.get(lease.pid) == agent:
                    del self._owners[lease.pid]

    # -- exclusive serialisation --------------------------------------------

    def lock_for(self, target_key: str) -> threading.Lock:
        """The single lock guarding ``target_key`` (created on first use).
        Every agent naming the same key shares the same lock object. The key is
        a ``bundle_id`` for the exclusive lane."""
        with self._guard:
            lock = self._locks.get(target_key)
            if lock is None:
                lock = self._locks[target_key] = threading.Lock()
            return lock

    @contextmanager
    def exclusive(self, bundle_id: str) -> Iterator[None]:
        """Run the body holding ``bundle_id``'s lock; concurrent exclusive
        agents on the same bundle queue and execute one at a time. This is the
        enforcement half of ``acquire(kind="exclusive")`` — the lease alone
        isolates nothing; wrap each action burst in this (or
        :meth:`ExclusiveHandle.exclusive`) to mutually-exclude."""
        lock = self.lock_for(bundle_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
