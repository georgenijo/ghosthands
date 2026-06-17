#!/usr/bin/env python3
"""Unit test: the name -> running-process fallback for apps LaunchServices
can't launch by name (dev builds, unsigned/ad-hoc .apps).

`_find_running_pid_by_name` is the matcher; it's pure given `list_apps`, so we
stub the driver and assert the selection logic (exact beats substring, skips
not-running, prefers a candidate with windows, None on no match). The live
bind+snapshot end of the fallback is environment-dependent and verified
manually, not here.

Run: python3 tests/test_resolve_name.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ghosthands import driver, read  # noqa: E402


def _stub_list_apps(apps):
    """Patch driver.call so list_apps returns `apps`; returns the original."""
    orig = driver.call

    def fake(tool, args=None, **kw):
        if tool == "list_apps":
            return {"apps": apps}
        raise AssertionError(f"unexpected driver.call({tool!r})")

    driver.call = fake
    return orig


def _find(apps, name):
    orig = _stub_list_apps(apps)
    try:
        return read._find_running_pid_by_name(name)
    finally:
        driver.call = orig


def test_exact_match_beats_substring() -> None:
    apps = [
        {"name": "FleetMapHelper", "pid": 222, "running": True, "windows": []},
        {"name": "FleetMap", "pid": 111, "running": True, "windows": []},
    ]
    assert _find(apps, "FleetMap") == 111


def test_case_insensitive() -> None:
    apps = [{"name": "FleetMap", "pid": 111, "running": True, "windows": []}]
    assert _find(apps, "fleetmap") == 111


def test_substring_when_no_exact() -> None:
    apps = [{"name": "FleetMap Dev", "pid": 333, "running": True, "windows": []}]
    assert _find(apps, "FleetMap") == 333


def test_skips_not_running() -> None:
    apps = [{"name": "FleetMap", "pid": 111, "running": False, "windows": []}]
    assert _find(apps, "FleetMap") is None


def test_none_on_no_match() -> None:
    apps = [{"name": "Finder", "pid": 1, "running": True, "windows": []}]
    assert _find(apps, "FleetMap") is None


def test_prefers_candidate_with_windows() -> None:
    apps = [
        {"name": "FleetMap", "pid": 100, "running": True, "windows": []},
        {"name": "FleetMap", "pid": 200, "running": True, "windows": [{"window_id": 9}]},
    ]
    assert _find(apps, "FleetMap") == 200


def main() -> int:
    tests = [
        test_exact_match_beats_substring,
        test_case_insensitive,
        test_substring_when_no_exact,
        test_skips_not_running,
        test_none_on_no_match,
        test_prefers_candidate_with_windows,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nALL {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
