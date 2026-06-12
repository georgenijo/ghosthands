"""`ghosthands doctor` — verify the Cua environment is green.

Checks, in dependency order:
1. cua-driver binary present; version matches the pinned one.
2. Persistent daemon running; if not, start it detached via LaunchServices
   (`open -n -g -a CuaDriver --args serve`) so its TCC identity is the
   driver's own (com.trycua.driver), not this terminal's.
3. Permissions read from the DAEMON's identity (`permissions status --json`,
   attribution must be `driver-daemon`). Accessibility is required; Screen
   Recording is optional — without it GhostHands runs cursor-less, which the
   AX action path does not need.
4. A timed end-to-end tool round-trip (get_screen_size).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from . import PINNED_DRIVER_VERSION, driver

OK, WARN, FAIL = "ok", "warn", "fail"
_ICONS = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}

DAEMON_START_CMD = ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"]
DAEMON_START_TIMEOUT = 8.0


@dataclass
class Check:
    name: str
    level: str
    detail: str
    fix: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str, fix: str | None = None) -> None:
        self.checks.append(Check(name, level, detail, fix))

    @property
    def green(self) -> bool:
        return all(c.level != FAIL for c in self.checks)

    def render(self) -> str:
        lines = ["GhostHands doctor"]
        for c in self.checks:
            lines.append(f"  {_ICONS[c.level]} {c.name}: {c.detail}")
            if c.fix and c.level != OK:
                lines.append(f"       fix: {c.fix}")
        warns = sum(1 for c in self.checks if c.level == WARN)
        verdict = "GREEN" if self.green else "RED"
        suffix = f" ({warns} warning{'s' if warns != 1 else ''})" if warns else ""
        lines.append(f"Environment: {verdict}{suffix}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "green": self.green,
                "checks": [c.__dict__ for c in self.checks],
            },
            indent=2,
        )


def _check_binary(report: Report) -> bool:
    code, out = driver.cli("--version", timeout=10)
    if code != 0:
        report.add(
            "binary", FAIL, f"cua-driver not runnable at {driver.DRIVER_BIN} ({out})",
            fix='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"',
        )
        return False
    version = out.split()[-1] if out else "unknown"
    if version != PINNED_DRIVER_VERSION:
        report.add(
            "binary", WARN,
            f"cua-driver {version} differs from pinned {PINNED_DRIVER_VERSION} (prerelease churn — retest before trusting)",
        )
    else:
        report.add("binary", OK, f"cua-driver {version} (pinned) at {driver.DRIVER_BIN}")
    return True


def _daemon_running() -> tuple[bool, str]:
    code, out = driver.cli("status", timeout=10)
    return code == 0, out


def _check_daemon(report: Report) -> bool:
    running, out = _daemon_running()
    if not running:
        try:
            subprocess.run(DAEMON_START_CMD, capture_output=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        deadline = time.monotonic() + DAEMON_START_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.5)
            running, out = _daemon_running()
            if running:
                break
    if running:
        detail = " ".join(line.strip() for line in out.splitlines()[1:]) or "running"
        report.add("daemon", OK, f"persistent daemon running ({detail})")
        return True
    report.add(
        "daemon", FAIL, "daemon not running and could not be started",
        fix="open -n -g -a CuaDriver --args serve",
    )
    return False


def _check_permissions(report: Report) -> None:
    code, out = driver.cli("permissions", "status", "--json", timeout=15)
    payload = None
    if code == 0:
        try:
            payload = json.loads(out[out.index("{"):])
        except (ValueError, json.JSONDecodeError):
            payload = None
    if payload is None:
        report.add(
            "permissions", FAIL, f"could not read permission status ({out.splitlines()[0] if out else 'no output'})",
            fix="cua-driver permissions status",
        )
        return

    attribution = (payload.get("source") or {}).get("attribution", "unknown")
    if attribution != "driver-daemon":
        report.add(
            "permissions", WARN,
            f"status attributed to {attribution!r}, not the driver daemon — grants may not apply to the driver (TCC attribution gotcha)",
            fix="cua-driver permissions grant",
        )

    if payload.get("accessibility"):
        report.add("accessibility", OK, "granted to com.trycua.driver (required)")
    else:
        report.add(
            "accessibility", FAIL, "NOT granted — AX actions will fail",
            fix="cua-driver permissions grant  (grants must attribute to the driver, not this terminal)",
        )

    if payload.get("screen_recording"):
        report.add("screen recording", OK, "granted (screenshots/cursor overlay available; AX path never needed it)")
    else:
        report.add(
            "screen recording", WARN,
            "not granted — optional. Running cursor-less (omit `session`); AX actions unaffected. "
            "Clicking WITH a session would throw EAGAIN until granted.",
            fix="cua-driver permissions grant  (only if you want screenshots / the visual cursor)",
        )


def _check_roundtrip(report: Report) -> None:
    started = time.monotonic()
    try:
        size = driver.call("get_screen_size", {}, timeout=15)
    except driver.DriverError as e:
        report.add("round-trip", FAIL, f"get_screen_size failed: {e}")
        return
    elapsed = time.monotonic() - started
    report.add(
        "round-trip", OK,
        f"get_screen_size in {elapsed:.2f}s — {size['width']:.0f}×{size['height']:.0f} @{size['scale_factor']:g}x",
    )


def run() -> Report:
    report = Report()
    if not _check_binary(report):
        return report
    if not _check_daemon(report):
        return report
    _check_permissions(report)
    _check_roundtrip(report)
    return report
