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

    p_bench = sub.add_parser("bench", help="benchmark contenders (passes args through to bench/run_bench.py)")
    p_bench.add_argument("bench_args", nargs=argparse.REMAINDER)

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
            from . import ownloop
            if not args.app:
                print("--brain local needs --app <bundle id> (the app to drive)")
                return 2
            brain = ownloop.LocalBrain(args.model)
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

    return 2


if __name__ == "__main__":
    sys.exit(main())
