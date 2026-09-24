"""agentops investigate | replay | report"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import ABLATIONS, POLICIES
from .evaluate import compare, investigate, replay, replay_check, sweep
from .tools import dump_trace, load_trace
from .world import generate

POLICY_BY_NAME = {p.name: p for p in POLICIES + ABLATIONS}


def _timeline(spans: list[dict]) -> str:
    rows = []
    for s in spans:
        t = f"{s['t_ms'] / 1000:7.2f}s"
        if s["kind"] == "alert":
            rows.append(f"{t}  alert on {s['service']}")
        elif s["kind"] == "tool":
            note = f" retry-after {s['retry_after_ms']} ms" if s["retry_after_ms"] else ""
            rows.append(f"{t}  {s['tool']}({s['service']}) #{s['attempt']}  {s['status']}  {s['latency_ms']} ms{note}")
        elif s["kind"] == "wait":
            rows.append(f"{t}  wait {s['ms']} ms  ({s['reason']})")
        else:
            rows.append(f"{t}  -> {s['message']}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("investigate", help="run one incident and print its trace")
    inv.add_argument("--incident", type=int, default=0)
    inv.add_argument("--seed", type=int, default=0)
    inv.add_argument("--policy", default="controlled", choices=sorted(POLICY_BY_NAME))
    inv.add_argument("--fault-rate", type=float, default=0.2)
    inv.add_argument("--trace", type=Path, help="write the spans as JSON lines")
    rep = sub.add_parser("replay", help="re-run a recorded trace offline with a (possibly different) policy")
    rep.add_argument("trace", type=Path)
    rep.add_argument("--policy", default="controlled", choices=sorted(POLICY_BY_NAME))
    out = sub.add_parser("report", help="policy comparison, fault-rate sweep and replay check")
    out.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args(argv)

    if args.command == "investigate":
        incident = generate(args.seed, args.incident + 1)[args.incident]
        report = investigate(POLICY_BY_NAME[args.policy], incident, args.fault_rate, args.seed)
        print(_timeline(report.spans))
        print(json.dumps(report.summary(), indent=2))
        print(f"ground truth: {incident.cause} on {incident.root}")
        if args.trace:
            dump_trace(report.spans, args.trace)
    elif args.command == "replay":
        spans = load_trace(args.trace)
        report = replay(POLICY_BY_NAME[args.policy], spans[0]["trace_id"], spans)
        print(json.dumps(report.summary(), indent=2))
        print("identical to the recording" if report.spans == spans else "decision trail differs from the recording")
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, result in (("evaluation", compare()), ("fault_sweep", sweep()), ("replay", replay_check())):
            (args.out / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"wrote {args.out / name}.json")


if __name__ == "__main__":
    main()
