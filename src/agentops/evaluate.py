"""Scoring investigations, the policy comparison, a fault-rate sweep, and trace replay."""

from __future__ import annotations

import statistics

from .agent import (
    ABLATIONS,
    POLICIES,
    RECENT_DEPLOY_MINUTES,
    Investigator,
    Policy,
    Report,
    log_signals,
    metric_signals,
)
from .tools import LiveBackend, ReplayBackend, ReplayDivergence, Session
from .world import Incident, generate


def investigate(policy: Policy, incident: Incident, fault_rate: float = 0.2, seed: int = 0, backend=None) -> Report:
    backend = backend or LiveBackend(incident, fault_rate, seed)
    session = Session(backend, incident.id, policy.call_budget)
    return Investigator(policy, session).run(incident.id, incident.alert)


def _pct(k: int, n: int) -> float:
    return round(100 * k / n, 2) if n else 0.0


def score(pairs: list[tuple[Incident, Report]]) -> dict:
    n = len(pairs)
    correct = [i for i, r in pairs if r.outcome == "diagnosed" and (r.service, r.cause) == (i.root, i.cause)]
    wrong = [(i, r) for i, r in pairs if r.outcome == "diagnosed" and (r.service, r.cause) != (i.root, i.cause)]
    escalated = [(i, r) for i, r in pairs if r.outcome == "escalated"]
    seconds = [r.elapsed_ms / 1000 for _, r in pairs]
    return {
        "incidents": n,
        "correct_pct": _pct(len(correct), n),
        "wrong_pct": _pct(len(wrong), n),
        "escalated_pct": _pct(len(escalated), n),
        "wrong_blamed_a_victim_pct": _pct(sum(r.service in i.path[:-1] for i, r in wrong), n),
        "escalations_with_root_evidence_missing_pct": _pct(
            sum(i.root_evidence_missing for i, _ in escalated), len(escalated)
        ),  # fmt: skip
        "escalations_naming_the_root_pct": _pct(sum(r.service == i.root for i, r in escalated), len(escalated)),
        "escalations_whose_suspected_cause_was_right_pct": _pct(
            sum((r.service, r.cause) == (i.root, i.cause) for i, r in escalated), len(escalated)
        ),
        "tool_calls_mean": round(statistics.fmean(r.tool_calls for _, r in pairs), 2),
        "failed_calls_mean": round(statistics.fmean(r.failed_calls for _, r in pairs), 2),
        "seconds_to_decision_mean": round(statistics.fmean(seconds), 2),
        "seconds_to_decision_p95": round(sorted(seconds)[int(0.95 * (n - 1))], 2),
    }


def _victim_looks_guilty(incident: Incident) -> bool:
    """Some service between the alert and the root shows an anomaly of its own (metric or log)."""
    for s in incident.path[:-1]:
        recent = any(d["minutes_before_alert"] <= RECENT_DEPLOY_MINUTES for d in incident.deploys[s])
        if metric_signals(incident.metrics[s], recent) or log_signals(incident.logs[s]):
            return True
    return False


def run_policy(policy: Policy, n: int, seeds, fault_rate: float) -> list[tuple[Incident, Report]]:
    pairs = []
    for seed in seeds:
        for incident in generate(seed, n):
            pairs.append((incident, investigate(policy, incident, fault_rate, seed)))
    return pairs


def compare(n: int = 400, seeds=(0, 1, 2), fault_rate: float = 0.2) -> dict:
    incidents = [i for seed in seeds for i in generate(seed, n)]
    return {
        "setup": {
            "incidents": len(incidents),
            "seeds": list(seeds),
            "fault_rate": fault_rate,
            "alert_is_the_root_pct": _pct(sum(i.alert == i.root for i in incidents), len(incidents)),
            "mean_hops_from_alert_to_root": round(statistics.fmean(len(i.path) - 1 for i in incidents), 2),
            "root_evidence_missing_pct": _pct(sum(i.root_evidence_missing for i in incidents), len(incidents)),
            "misleading_signal_on_a_victim_pct": _pct(sum(_victim_looks_guilty(i) for i in incidents), len(incidents)),
        },
        "policies": {p.name: score(run_policy(p, n, seeds, fault_rate)) for p in POLICIES + ABLATIONS},
    }


def sweep(fault_rates=(0.0, 0.1, 0.2, 0.3, 0.4), n: int = 400, seeds=(0, 1, 2)) -> dict:
    return {
        "fault_rates": list(fault_rates),
        "policies": {p.name: [score(run_policy(p, n, seeds, f)) for f in fault_rates] for p in POLICIES + ABLATIONS},
    }


def replay(policy: Policy, incident_id: str, spans: list[dict]) -> Report:
    alert = next(s["service"] for s in spans if s["kind"] == "alert")
    session = Session(ReplayBackend(spans), incident_id, policy.call_budget)
    return Investigator(policy, session).run(incident_id, alert)


def replay_check(n: int = 400, seed: int = 0, fault_rate: float = 0.2) -> dict:
    """Record the controlled policy's traces once, then replay every policy against them offline."""
    recorder = next(p for p in POLICIES if p.name == "controlled")
    recorded = [(i, investigate(recorder, i, fault_rate, seed)) for i in generate(seed, n)]
    out = {"recorded_with": recorder.name, "traces": len(recorded), "tool_calls_recorded": 0, "replays": {}}
    out["tool_calls_recorded"] = sum(r.tool_calls for _, r in recorded)
    for policy in POLICIES + ABLATIONS:
        identical = same = changed = diverged = 0
        for incident, original in recorded:
            try:
                again = replay(policy, incident.id, original.spans)
            except ReplayDivergence:
                diverged += 1  # needs data the recording does not have: shadow-run it live instead
                continue
            identical += again.spans == original.spans
            if (again.outcome, again.service, again.cause) == (original.outcome, original.service, original.cause):
                same += 1
            else:
                changed += 1  # a decision change to review before shipping
        out["replays"][policy.name] = {
            "identical_trace": identical,
            "same_decision": same,
            "different_decision": changed,
            "diverged_from_recording": diverged,
        }
    return out
