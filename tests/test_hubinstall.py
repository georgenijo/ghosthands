"""Hermetic tests for hub install/uninstall sequencing + the parsers.

No real claude/codex binary is touched: the Installer's runner and registration
readers are injected with fakes, so we prove the *logic* (idempotency, backup,
restore, the exact CLI commands issued) deterministically. The live wiring is
proven separately against the real binaries.

Run: .venv/bin/python tests/test_hubinstall.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghosthands import hubinstall as hi  # noqa: E402


CLAUDE_GET_RAW = """\
cua-driver:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: /Users/me/.local/bin/cua-driver
  Args: mcp
  Environment:
"""

CLAUDE_GET_HUB = """\
cua-driver:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: /Users/me/.local/bin/ghosthands
  Args: hub
  Environment:
"""

CLAUDE_GET_USER = """\
cua-driver:
  Scope: User config (available in all your projects)
  Type: stdio
  Command: /Users/me/.local/bin/cua-driver
  Args: mcp
"""


def test_parse_claude_get():
    s = hi._parse_claude_get(CLAUDE_GET_RAW)
    assert s == {"command": "/Users/me/.local/bin/cua-driver", "args": "mcp",
                 "scope": "local"}, s
    assert hi._parse_claude_get(CLAUDE_GET_USER)["scope"] == "user"
    assert hi._parse_claude_get("No MCP server found with name: cua-driver") is None
    print("PASS parse_claude_get")


def test_is_hub_routed():
    assert hi.is_hub_routed(hi._parse_claude_get(CLAUDE_GET_HUB)) is True
    assert hi.is_hub_routed(hi._parse_claude_get(CLAUDE_GET_RAW)) is False
    assert hi.is_hub_routed(None) is False
    # `<python> -m ghosthands hub` shape also counts as hub-routed.
    assert hi.is_hub_routed({"command": "python", "args": "-m ghosthands hub"}) is True
    print("PASS is_hub_routed")


def test_command_builders():
    assert hi.claude_add_cmd("cua-driver", "local", ["/x/ghosthands", "hub"]) == [
        "claude", "mcp", "add", "cua-driver", "-s", "local", "--",
        "/x/ghosthands", "hub"]
    assert hi.claude_remove_cmd("cua-driver", "local") == [
        "claude", "mcp", "remove", "cua-driver", "-s", "local"]
    print("PASS command_builders")


class _FakeClaude:
    """A fake `claude mcp` whose registration state mutates with add/remove,
    so install/uninstall can be driven end-to-end with no real binary."""

    def __init__(self, server: dict | None):
        self.server = server
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:3] == ["claude", "mcp", "remove"]:
            if self.server is None:
                return 1, "No MCP server found"
            self.server = None
            return 0, "Removed"
        if argv[:3] == ["claude", "mcp", "add"]:
            # argv: claude mcp add <name> -s <scope> -- <cmd> <args...>
            i = argv.index("--")
            cmd, *rest = argv[i + 1:]
            scope = argv[argv.index("-s") + 1]
            self.server = {"command": cmd, "args": " ".join(rest), "scope": scope}
            return 0, "Added"
        return 1, "unexpected"

    def get(self, name: str) -> dict | None:
        return self.server


def _installer(fake: _FakeClaude) -> hi.Installer:
    # no-op backup hooks so the test never touches ~/.ghosthands
    saved: dict = {}

    def backup(client, name, server):
        saved[(client, name)] = server

    def pop_backup(client, name):
        return saved.pop((client, name), None)

    return hi.Installer(run=fake.run, claude_get=fake.get,
                        codex_get=lambda n: None, backup=backup,
                        pop_backup=pop_backup)


def test_install_then_uninstall_roundtrip():
    raw = {"command": "/Users/me/.local/bin/cua-driver", "args": "mcp", "scope": "local"}
    fake = _FakeClaude(dict(raw))
    inst = _installer(fake)

    r = inst.install_claude("cua-driver", dry_run=True)
    assert r.message == "dry-run" and r.changed is False
    assert fake.server == raw, "dry-run must not mutate registration"

    r = inst.install_claude("cua-driver")
    assert r.ok and r.changed, r
    assert hi.is_hub_routed(fake.server), fake.server
    assert fake.server["scope"] == "local", "scope preserved across re-register"
    # remove-then-add issued, in that order
    kinds = [c[2] for c in fake.calls if c[:2] == ["claude", "mcp"]]
    assert kinds == ["remove", "add"], kinds

    # idempotent: installing again is a no-op
    n_before = len(fake.calls)
    r2 = inst.install_claude("cua-driver")
    assert not r2.changed and r2.ok and "already" in r2.message
    assert len(fake.calls) == n_before, "idempotent install issued extra commands"

    # uninstall restores the exact prior command from the backup
    r3 = inst.uninstall_claude("cua-driver")
    assert r3.ok, r3
    assert not hi.is_hub_routed(fake.server)
    assert fake.server["command"] == raw["command"]
    assert fake.server["args"] == raw["args"]
    print("PASS install_then_uninstall_roundtrip")


def test_install_when_absent_uses_local_and_restores_canonical():
    fake = _FakeClaude(None)  # no prior registration
    inst = _installer(fake)
    r = inst.install_claude("cua-driver")
    assert r.ok and hi.is_hub_routed(fake.server)
    assert fake.server["scope"] == "local"
    # no backup existed -> uninstall restores canonical <DRIVER_BIN> mcp
    r2 = inst.uninstall_claude("cua-driver")
    assert r2.ok and not hi.is_hub_routed(fake.server)
    assert fake.server["args"] == "mcp"
    print("PASS install_when_absent")


def test_ghclaude_script_shape():
    assert GHCLAUDE_TAG_LINE in hi.GHCLAUDE_SCRIPT
    assert hi.GHCLAUDE_SCRIPT.startswith("#!/bin/sh")
    assert 'exec env GHOSTHANDS_AGENT="$tag" claude "$@"' in hi.GHCLAUDE_SCRIPT
    print("PASS ghclaude_script_shape")


GHCLAUDE_TAG_LINE = 'tag="$1"'


if __name__ == "__main__":
    test_parse_claude_get()
    test_is_hub_routed()
    test_command_builders()
    test_install_then_uninstall_roundtrip()
    test_install_when_absent_uses_local_and_restores_canonical()
    test_ghclaude_script_shape()
    print("\nALL hubinstall tests PASS")
