"""The reliable action wrapper — GhostHands' core hardening layer.

Encodes the golden workflow (AGENTS.md) so callers never deal with the
prerelease gotchas directly:

  launch (background) -> snapshot (AX, no screenshot) -> act by element_index
  -> snapshot again to verify

Hardening, per DESIGN.md §8:
- Re-snapshots immediately before EVERY element action (index cache expires
  in seconds).
- A matcher, not a raw index, names the target; it is resolved against the
  fresh snapshot each attempt, and all duplicate-subtree candidates are tried
  in order (a snapshot can render the same window twice with different index
  ranges).
- On EAGAIN / daemon-closed-connection the action often landed anyway: with a
  `verify` predicate the wrapper checks state before re-issuing; without one
  it re-issues once (pass `verify` for non-idempotent actions).
- Always cursor-less: no `session` is ever sent, so nothing here needs
  Screen Recording.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import ax, driver
from .driver import StaleIndexError, TransientDriverError

Matcher = Callable[[ax.Element], bool]
Predicate = Callable[["Snapshot"], bool]

DEFAULT_RETRIES = 2
SETTLE_SECONDS = 0.25  # let the UI settle between an action and its verify
WINDOW_POLL_SECONDS = 0.4
WINDOW_POLL_ATTEMPTS = 10


class GhostHandsError(RuntimeError):
    pass


class ElementNotFoundError(GhostHandsError):
    pass


class VerificationError(GhostHandsError):
    pass


def _as_matcher(target: str | Matcher) -> Matcher:
    if callable(target):
        return target
    wanted = target.casefold()

    def match(el: ax.Element) -> bool:
        return el.text.casefold() == wanted or (el.ax_id or "").casefold() == wanted

    return match


@dataclass
class Snapshot:
    pid: int
    window_id: int
    markdown: str
    elements: list[ax.Element]

    def find_all(self, target: str | Matcher, *, actionable: bool = True) -> list[ax.Element]:
        match = _as_matcher(target)
        found = [
            el
            for el in self.elements
            if match(el) and (el.index is not None or not actionable)
        ]
        found.sort(key=lambda el: (el.subtree, el.index if el.index is not None else -1))
        return found

    def find(self, target: str | Matcher, *, actionable: bool = True) -> ax.Element:
        found = self.find_all(target, actionable=actionable)
        if not found:
            raise ElementNotFoundError(
                f"no element matching {target!r} in window {self.window_id} (pid {self.pid})"
            )
        return found[0]

    def value_of(self, target: str | Matcher) -> str:
        """Value/text of the first matching node, actionable or not.
        Matches on label/title so `value_of('Edit field')` works on
        `AXStaticText = "42" (Edit field)`."""
        match = _as_matcher(target)
        for el in self.elements:
            if match(el) or (el.label or "").casefold() == str(target).casefold():
                return el.value if el.value is not None else el.text
        raise ElementNotFoundError(f"no node matching {target!r}")


class App:
    """A (pid, window_id) target plus the hardened verbs that act on it."""

    def __init__(self, pid: int, window_id: int, *, name: str = "", retries: int = DEFAULT_RETRIES):
        self.pid = pid
        self.window_id = window_id
        self.name = name
        self.retries = retries

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def launch(cls, bundle_id: str, *, urls: list[str] | None = None,
               title_contains: str | None = None, retries: int = DEFAULT_RETRIES) -> "App":
        """Launch in the background (no focus steal) and bind to the on-screen
        window. `title_contains` targets a specific window when an app has many
        (e.g. a browser with several tabs/windows) — it is polled for, since a
        freshly opened tab's title appears a beat after launch."""
        args: dict = {"bundle_id": bundle_id}
        if urls:
            args["urls"] = urls
        result = driver.call("launch_app", args)
        pid = result["pid"]
        name = result.get("name", bundle_id)

        window = cls._pick_window(result.get("windows", []), title_contains)
        for _ in range(WINDOW_POLL_ATTEMPTS):
            if window is not None:
                break
            time.sleep(WINDOW_POLL_SECONDS)
            listed = driver.call("list_windows", {"pid": pid})
            windows = listed["windows"] if isinstance(listed, dict) else listed
            window = cls._pick_window(windows or [], title_contains)
        if window is None:
            raise GhostHandsError(
                f"{bundle_id} (pid {pid}): no on-screen window"
                + (f" titled ~{title_contains!r}" if title_contains else "") + " appeared")
        return cls(pid, window["window_id"], name=name, retries=retries)

    @staticmethod
    def _pick_window(windows: list[dict], title_contains: str | None = None) -> dict | None:
        on_screen = [w for w in windows if w.get("is_on_screen")]
        if title_contains:
            matches = [w for w in on_screen if title_contains.casefold() in (w.get("title") or "").casefold()]
            return matches[-1] if matches else None  # newest matching window/tab
        # Prefer titled windows; apps surface phantom untitled menubar windows.
        titled = [w for w in on_screen if w.get("title")]
        if titled:
            return titled[0]
        return on_screen[0] if on_screen else None

    # -- observation ---------------------------------------------------------

    def snapshot(self, query: str | None = None) -> Snapshot:
        args: dict = {"pid": self.pid, "window_id": self.window_id, "capture_mode": "ax"}
        if query:
            args["query"] = query
        result = driver.call("get_window_state", args)
        markdown = result.get("tree_markdown", "")
        return Snapshot(
            pid=self.pid,
            window_id=self.window_id,
            markdown=markdown,
            elements=ax.parse_tree(markdown),
        )

    def read(self, target: str | Matcher, *, query: str | None = None) -> str:
        return self.snapshot(query=query).value_of(target)

    def wait_for(self, predicate: Predicate, *, timeout: float = 5.0, interval: float = 0.4,
                 query: str | None = None) -> Snapshot:
        deadline = time.monotonic() + timeout
        while True:
            snap = self.snapshot(query=query)
            if predicate(snap):
                return snap
            if time.monotonic() >= deadline:
                raise VerificationError(f"condition not met within {timeout}s")
            time.sleep(interval)

    # -- actions -------------------------------------------------------------

    def click(self, target: str | Matcher, *, verify: Predicate | None = None) -> ax.Element:
        """Snapshot-fresh click by matcher. Tries every duplicate-subtree
        candidate on stale-index misses; on transient daemon errors consults
        `verify` (the action often landed) before re-issuing."""
        return self._element_action("click", target, verify=verify)

    def _element_action(self, tool: str, target: str | Matcher, *,
                        verify: Predicate | None, **extra) -> ax.Element:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(SETTLE_SECONDS)
            snap = self.snapshot()
            candidates = snap.find_all(target)
            if not candidates:
                last_err = ElementNotFoundError(
                    f"no element matching {target!r} in window {self.window_id}"
                )
                continue
            for el in candidates:
                try:
                    driver.call(tool, {
                        "pid": self.pid,
                        "window_id": self.window_id,
                        "element_index": el.index,
                        **extra,
                    })
                    self._post_verify(verify)
                    return el
                except StaleIndexError as e:
                    last_err = e  # try the duplicate index set
                    continue
                except TransientDriverError as e:
                    last_err = e
                    if verify is not None and self._landed(verify):
                        return el
                    break  # re-snapshot and retry the whole action
        raise last_err if last_err else GhostHandsError(f"{tool} on {target!r} failed")

    def _pid_action(self, tool: str, args: dict, *, verify: Predicate | None = None) -> None:
        """Non-element action (keys/text go to the pid). Same transient-retry
        contract as element actions."""
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(SETTLE_SECONDS)
            try:
                driver.call(tool, {"pid": self.pid, "window_id": self.window_id, **args})
                self._post_verify(verify)
                return
            except TransientDriverError as e:
                last_err = e
                if verify is not None and self._landed(verify):
                    return
        raise last_err if last_err else GhostHandsError(f"{tool} failed")

    def type_text(self, text: str, *, verify: Predicate | None = None) -> None:
        self._pid_action("type_text", {"text": text}, verify=verify)

    def press_key(self, key: str, *, verify: Predicate | None = None) -> None:
        self._pid_action("press_key", {"key": key}, verify=verify)

    def hotkey(self, keys: list[str], *, verify: Predicate | None = None) -> None:
        self._pid_action("hotkey", {"keys": keys}, verify=verify)

    # -- internals -----------------------------------------------------------

    def _landed(self, verify: Predicate) -> bool:
        time.sleep(SETTLE_SECONDS)
        try:
            return verify(self.snapshot())
        except driver.DriverError:
            return False

    def _post_verify(self, verify: Predicate | None) -> None:
        if verify is None:
            return
        self.wait_for(verify, timeout=3.0)
