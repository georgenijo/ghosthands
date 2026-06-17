"""`ghosthands` CLI — user-facing surface.

Subcommands:
  doctor   verify/repair the Cua environment; exit 0 when green
  smoke    drive Calculator to 7 × 6 = 42 through the action wrapper
  brains   detect authorized brains (Claude Code / Codex CLI / API keys)
  run      dispatch a goal to a brain wired to the Cua hands + skill
  bench    run the brain-vs-brain benchmark suite (see bench/run_bench.py)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["bench"]:  # REMAINDER can't take leading --options; pass through directly
        script = Path(__file__).resolve().parent.parent.parent / "bench" / "run_bench.py"
        return subprocess.run([sys.executable, str(script), *argv[1:]]).returncode

    parser = argparse.ArgumentParser(
        prog="ghosthands",
        description="Model-agnostic local macOS computer-use harness (hands: Cua Driver).",
    )
    parser.add_argument("--version", action="version", version=f"ghosthands {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="verify the Cua environment (binary, daemon, permissions, round-trip)")
    p_doctor.add_argument("--json", action="store_true", help="emit a machine-readable report")

    sub.add_parser("smoke", help="prove the wrapper: Calculator 7 × 6 = 42, cursor-less")

    sub.add_parser("brains", help="detect authorized brains and show the auto pick")

    p_run = sub.add_parser("run", help='run a goal: ghosthands run "<goal>" [--brain claude|gpt|local|auto]')
    p_run.add_argument("goal")
    p_run.add_argument("--brain", default="auto", help="claude | gpt | auto (subscription) | local (free MLX brain on the AX tree)")
    p_run.add_argument("--timeout", type=float, default=600.0)
    p_run.add_argument("--app", help="bundle id to drive (required for --brain local)")
    p_run.add_argument("--url", help="URL to open first (browser tasks, --brain local)")
    p_run.add_argument("--title", help="window-title substring to target (--brain local)")
    p_run.add_argument("--model", help="MLX model id for --brain local (default: project default)")
    p_run.add_argument("--max-turns", type=int, default=20)
    p_run.add_argument("--surface", choices=["auto", "web", "native"], default="auto",
                       help="(--brain local) tier: web=DOM/CDP, native=AX, "
                            "auto=route by app/snapshot (issue #9)")
    p_run.add_argument("--debug-port", type=int, default=9333,
                       help="(--surface web) Chromium remote-debugging port")

    p_loop = sub.add_parser("ownloop", help="standalone brain loop: local (free MLX) | claude-api | gpt-api")
    p_loop.add_argument("goal")
    p_loop.add_argument("--brain", default="local", help="local (free, default) | claude-api | gpt-api")
    p_loop.add_argument("--app", default="com.apple.calculator", help="bundle id to drive")
    p_loop.add_argument("--model", help="MLX model id for --brain local")
    p_loop.add_argument("--max-turns", type=int, default=20)

    p_replay = sub.add_parser("replay", help="replay a recorded flow with NO model: ghosthands replay <flow>")
    p_replay.add_argument("flow", help="flow name (flows/<name>.json) or path")
    p_replay.add_argument("--heal", action="store_true", help="self-heal a missing target with one local-model call")
    p_replay.add_argument("--model", help="MLX model id for --heal")

    p_record = sub.add_parser("record", help='record a flow once with the local brain: ghosthands record <name> "<goal>" --app BUNDLE')
    p_record.add_argument("name")
    p_record.add_argument("goal")
    p_record.add_argument("--app", required=True, help="bundle id to drive")
    p_record.add_argument("--url", help="URL to open first (browser flows)")
    p_record.add_argument("--title", help="window-title substring to target")
    p_record.add_argument("--model", help="MLX model id for the local brain")
    p_record.add_argument("--max-turns", type=int, default=12)

    p_skills = sub.add_parser("skills", help="agent operating instructions, version-matched to this install: ghosthands skills [get] [core|web|canvas|tests]")
    p_skills.add_argument("name", nargs="*", help="skill name (omit to list); 'get <name>' also accepted")

    p_scene = sub.add_parser("scene", help='generate an Excalidraw scene on the routed model: ghosthands scene "<description>"')
    p_scene.add_argument("description")
    p_scene.add_argument("--raw", action="store_true", help="print the raw element array (localStorage recipe) instead of the clipboard payload")
    p_scene.add_argument("--model", help="MLX model id (default: scene specialist)")

    p_bench = sub.add_parser("bench", help="benchmark contenders (passes args through to bench/run_bench.py)")
    p_bench.add_argument("bench_args", nargs=argparse.REMAINDER)

    p_monitor = sub.add_parser("monitor", help="read-only dashboard of who is driving the hands: ghosthands monitor [--port 7878]")
    p_monitor.add_argument("--port", type=int, default=7878)
    p_monitor.add_argument("--once", action="store_true", help="print the current state as JSON and exit (no server)")

    p_compact = sub.add_parser("compact", help="compact a fat AX snapshot at the funnel: ghosthands compact <file.json|.md>")
    p_compact.add_argument("file", help="a get_window_state JSON (with tree_markdown) or a raw AX markdown file")
    p_compact.add_argument("--max-chars", type=int, default=8000, help="offload the full original to disk above this size")
    p_compact.add_argument("--stats-only", action="store_true", help="print only the reduction stats, not the compacted text")

    p_hub = sub.add_parser("hub", help="stdio MCP hub: bare = run the proxy; install/uninstall/status manage routing")
    hub_sub = p_hub.add_subparsers(dest="hub_command")
    p_hi = hub_sub.add_parser("install", help="route a client's cua-driver MCP through the hub + write the ghclaude launcher")
    p_hi.add_argument("--client", choices=["claude", "codex", "both"], default="claude", help="which agent to re-register (default: claude)")
    p_hi.add_argument("--name", default="cua-driver", help="MCP server name to re-register (default: cua-driver, keeps tool names stable)")
    p_hi.add_argument("--scope", help="claude scope: local|user|project (default: keep the current one)")
    p_hi.add_argument("--dry-run", action="store_true", help="print the commands that would run, change nothing")
    p_hi.add_argument("--no-ghclaude", action="store_true", help="don't write the ghclaude helper")
    p_hu = hub_sub.add_parser("uninstall", help="restore the raw cua-driver MCP (undo install)")
    p_hu.add_argument("--client", choices=["claude", "codex", "both"], default="claude")
    p_hu.add_argument("--name", default="cua-driver")
    p_hu.add_argument("--scope")
    p_hs = hub_sub.add_parser("status", help="show whether cua-driver is hub-routed, the launcher, and recent tagged logs")
    p_hs.add_argument("--name", default="cua-driver")

    p_snapshot = sub.add_parser("snapshot", help="no-brain AX-tree dump: ghosthands snapshot <bundle|pid|name> [--ax|--json|--watch]")
    p_snapshot.add_argument("target", help="bundle id (com.apple.calculator), bare pid, or process name")
    fmt_grp = p_snapshot.add_mutually_exclusive_group()
    fmt_grp.add_argument("--ax", action="store_true", help="markdown AX tree (default)")
    fmt_grp.add_argument("--json", action="store_true", help="parsed elements as JSON")
    p_snapshot.add_argument("--watch", action="store_true", help="re-dump when the tree changes (Ctrl-C to stop)")
    p_snapshot.add_argument("--query", help="case-insensitive filter for the tree (matching lines + ancestors)")
    p_snapshot.add_argument("--title", help="window-title substring to target (apps with many windows)")

    p_shot = sub.add_parser("shot", help="screenshot a window to a PNG via cua's grant: ghosthands shot <target> <file.png>")
    p_shot.add_argument("target", help="bundle id, bare pid, or process name")
    p_shot.add_argument("file", help="output .png path")
    p_shot.add_argument("--title", help="window-title substring to target")

    p_find = sub.add_parser("find", help="resolve a name to an element (role/on-screen/index), no model: ghosthands find <name> <target>")
    p_find.add_argument("name", help="accessible name / label / title / ax_id to resolve")
    p_find.add_argument("target", help="bundle id, bare pid, or process name")
    p_find.add_argument("--title", help="window-title substring to target")

    p_web = sub.add_parser("web", help="DOM tier over Brave's debug port: ghosthands web targets|open ...")
    web_sub = p_web.add_subparsers(dest="web_command", required=True)
    p_web_t = web_sub.add_parser("targets", help="list browser tabs by CDP target id (no fronting)")
    p_web_t.add_argument("--port", type=int, default=9333)
    p_web_o = web_sub.add_parser("open", help="launch Brave with the debug port open on a URL")
    p_web_o.add_argument("url")
    p_web_o.add_argument("--port", type=int, default=9333)

    args = parser.parse_args(argv)

    if args.command == "doctor":
        from . import doctor
        report = doctor.run()
        print(report.to_json() if args.json else report.render())
        return 0 if report.green else 1

    if args.command == "smoke":
        from . import smoke
        return 0 if smoke.run() else 1

    if args.command == "brains":
        from . import brains
        detected = brains.detect_all()
        for b in detected:
            mark = "✅" if (b.available and b.launchable) else ("🔑" if b.available else "❌")
            print(f"  {mark} {b.name:<10} {b.label:<14} [{b.kind}] {b.detail}")
        try:
            pick = brains.select("auto")
            print(f"auto pick: {pick.name} ({pick.label})")
            return 0
        except SystemExit as e:
            print(f"auto pick: none ({e})")
            return 1

    if args.command == "run":
        if args.brain == "local":
            from . import ownloop, webtier
            if not args.app and args.surface != "web":
                print("--brain local needs --app <bundle id> (the app to drive)")
                return 2
            surface = args.surface
            if surface == "auto":
                surface = webtier.route_surface(bundle_id=args.app)
            brain = ownloop.LocalBrain(args.model)
            if surface == "web":
                if not args.url:
                    print("--surface web needs --url <page> (the web page to drive)")
                    return 2
                from . import webloop
                print(f"brain: local ({brain.model_id}) — free, DOM tier (web) [#9]")
                print(f"goal:  {args.goal}\n")
                done = webloop.run_web_loop(
                    brain, args.goal, args.url, port=args.debug_port,
                    max_turns=args.max_turns)
                return 0 if done else 1
            print(f"brain: local ({brain.model_id}) — free, on the AX tree")
            print(f"goal:  {args.goal}\n")
            done = ownloop.run_loop(
                brain, args.goal, args.app, max_turns=args.max_turns,
                urls=[args.url] if args.url else None, title_contains=args.title)
            return 0 if done else 1
        from . import brains
        brain = brains.select(args.brain)
        print(f"brain: {brain.name} ({brain.label}) — {brain.detail}")
        print(f"goal:  {args.goal}\n")
        result = brains.run_goal(brain, args.goal, timeout=args.timeout, stream=print)
        print(f"\n[{brain.name}] exit={result.returncode} wall={result.seconds:.1f}s")
        return 0 if result.returncode == 0 else 1

    if args.command == "skills":
        skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
        if not skills_dir.is_dir():
            print("skills directory not found (non-editable install?) — "
                  "see https://github.com/georgenijo/ghosthands/tree/main/skills",
                  file=sys.stderr)
            return 1
        names = {"core": "SKILL.md", "web": "WEB_APPS.md",
                 "canvas": "CANVAS.md", "tests": "TESTS.md"}
        words = [w for w in args.name if w != "get"]
        if not words:
            print("skills (read with: ghosthands skills get <name>):")
            for k, f in names.items():
                first = (skills_dir / f).read_text().splitlines()[0].lstrip("# ")
                print(f"  {k:<8} {f:<13} {first}")
            return 0
        f = names.get(words[0])
        if f is None:
            print(f"unknown skill {words[0]!r} (known: {', '.join(names)})",
                  file=sys.stderr)
            return 1
        print((skills_dir / f).read_text())
        return 0

    if args.command == "scene":
        from . import scene
        try:
            elements = scene.generate_scene(args.description, model=args.model)
        except scene.SceneError as e:
            print(f"scene generation failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(elements) if args.raw
              else scene.clipboard_payload(elements))
        return 0

    if args.command == "ownloop":
        from . import ownloop
        brain_cls = ownloop.BRAINS.get(args.brain)
        if brain_cls is None:
            print(f"unknown brain {args.brain!r} (known: {', '.join(ownloop.BRAINS)})")
            return 2
        brain = ownloop.LocalBrain(args.model) if args.brain == "local" else brain_cls()
        done = ownloop.run_loop(brain, args.goal, args.app, max_turns=args.max_turns)
        return 0 if done else 1

    if args.command == "replay":
        from . import flows
        flow = flows.load(args.flow)
        heal = None
        if args.heal:
            from . import ownloop
            heal = ownloop.LocalBrain(args.model)
        ok = flows.replay(flow, heal_brain=heal)
        print(f"replay {'PASS' if ok else 'FAIL'}: {flow.name} ({len(flow.steps)} steps, no model)")
        return 0 if ok else 1

    if args.command == "record":
        from . import flows, ownloop
        brain = ownloop.LocalBrain(args.model)
        flow, ok = flows.record(
            brain, args.name, args.goal, args.app,
            urls=[args.url] if args.url else None, title_contains=args.title,
            max_turns=args.max_turns)
        print(f"recorded {flow.name}: {len(flow.steps)} steps -> {flow.path()} (succeeded={ok})")
        return 0 if ok else 1

    if args.command == "bench":
        script = Path(__file__).resolve().parent.parent.parent / "bench" / "run_bench.py"
        return subprocess.run([sys.executable, str(script), *args.bench_args]).returncode

    if args.command == "monitor":
        from . import monitor
        if args.once:
            print(json.dumps(monitor.state(), indent=2))
            return 0
        print(f"GhostHands monitor — http://localhost:{args.port}  (Ctrl-C to stop)")
        try:
            monitor.serve(args.port)
        except KeyboardInterrupt:
            pass
        return 0

    if args.command == "compact":
        from . import compaction
        raw = Path(args.file).read_text()
        markdown = raw
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "tree_markdown" in obj:
                markdown = obj["tree_markdown"]
        except (ValueError, json.JSONDecodeError):
            pass
        res = compaction.compact(markdown, max_chars=args.max_chars)
        print(f"# {res['original_chars']} -> {res['compact_chars']} chars "
              f"({res['reduction_pct']:.1f}% saved)"
              + (f"  full original: {res['handle']}" if res.get("handle") else ""))
        if not args.stats_only:
            print(res["text"])
        return 0

    if args.command == "hub":
        if getattr(args, "hub_command", None) is None:
            from . import hub  # bare `ghosthands hub` = run the proxy (the MCP command)
            return hub.main()
        from . import hubinstall
        if args.hub_command == "install":
            return hubinstall.cli_install(args)
        if args.hub_command == "uninstall":
            return hubinstall.cli_uninstall(args)
        if args.hub_command == "status":
            return hubinstall.cli_status(args)
        return 2

    if args.command == "snapshot":
        from . import read  # no-brain read tier (imports neither ownloop nor mlx)
        fmt = "json" if args.json else "ax"
        try:
            if args.watch:
                return read.watch(args.target, fmt=fmt, query=args.query,
                                  title_contains=args.title)
            return read.snapshot(args.target, fmt=fmt, query=args.query,
                                 title_contains=args.title)
        except read.GhostHandsError as e:
            print(f"snapshot failed: {e}", file=sys.stderr)
            return 1

    if args.command == "shot":
        from . import read
        return read.shot(args.target, args.file, title_contains=args.title)

    if args.command == "find":
        from . import read
        return read.find(args.name, args.target, title_contains=args.title)

    if args.command == "web":
        from . import webtier
        if args.web_command == "open":
            result = webtier.launch_web(args.url, port=args.port)
            print(f"launched {result.get('name', 'browser')} pid={result.get('pid')} "
                  f"debug-port={args.port} url={args.url}")
            return 0
        if args.web_command == "targets":
            targets = webtier.list_targets(args.port)
            if not targets:
                print(f"no targets on debug port {args.port} "
                      f"(launch with: ghosthands web open <url> --port {args.port})",
                      file=sys.stderr)
                return 1
            for t in targets:
                tid = (t.get("id") or "?")[:8]
                print(f"  {tid}  {t.get('type', ''):<10} "
                      f"{(t.get('title') or '')[:40]!r}  {t.get('url', '')}")
            return 0
        return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
