"""Diagnosis rules and the investigation policies being compared.

All policies share the same diagnosis rules; they differ only in control flow:
whether they follow the error trail down the call graph, how they retry,
whether they check that metrics are fresh, and whether they need metrics and
logs to agree before naming a root cause. The rules stand in for an LLM's
reading of the telemetry; the control flow around them is the subject here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .tools import Session
from .world import CAUSES, LOG_SIGNATURES, kind

RECENT_DEPLOY_MINUTES = 30
STALE_AFTER_MS = 5 * 60 * 1000
BACKOFF_MS = 200
LOG_MIN_COUNT = 3

METRIC_TESTS = {  # cause -> (metric, comparison, threshold)
    "bad_deploy": ("error_rate", ">", 0.05),  # plus a deploy in the last 30 minutes
    "memory_leak": ("restarts", ">=", 2),  # plus memory above 90%
    "cpu_throttling": ("cpu_throttle", ">", 0.3),
    "cert_expiry": ("tls_errors_per_min", ">", 20),
    "connection_exhaustion": ("connections_pct", ">=", 95),
    "disk_full": ("disk_pct", ">=", 95),
    "eviction_storm": ("hit_rate", "<", 0.6),
}
_HOP = re.compile(r"^(?:timeout calling|upstream error from) (\S+)$")


def metric_signals(metrics: dict, recent_deploy: bool) -> set[str]:
    found = set()
    for cause, (key, op, threshold) in METRIC_TESTS.items():
        if key not in metrics:
            continue
        value = metrics[key]
        if {">": value > threshold, ">=": value >= threshold, "<": value < threshold}[op]:
            found.add(cause)
    if "bad_deploy" in found and not recent_deploy:
        found.discard("bad_deploy")
    if "memory_leak" in found and metrics.get("memory_pct", 0) <= 90:
        found.discard("memory_leak")
    return found


def log_signals(lines: list[str]) -> Counter:
    counts = Counter(cause for cause, sig in LOG_SIGNATURES.items() for line in lines if line == sig)
    return Counter({c: n for c, n in counts.items() if n >= LOG_MIN_COUNT})


def next_hop(lines: list[str]) -> str | None:
    hops = Counter(m.group(1) for line in lines if (m := _HOP.match(line)))
    return min(hops, key=lambda s: (-hops[s], s)) if hops else None


@dataclass(frozen=True)
class Policy:
    name: str
    descend: bool = True  # follow the error trail down the call graph
    retry: str = "classified"  # none | naive (retry anything at once) | classified (backoff, honour retry-after)
    check_staleness: bool = True  # reject metrics whose as_of predates the incident
    evidence_gate: bool = True  # name a cause only when metrics and logs agree; otherwise escalate
    max_attempts: int = 3
    call_budget: int = 30


POLICIES = (
    Policy("single pass", descend=False, retry="none", check_staleness=False, evidence_gate=False),
    Policy("retry everything", retry="naive", check_staleness=False, evidence_gate=False),
    Policy("controlled"),
)
ABLATIONS = (
    Policy("controlled, naive retries", retry="naive"),
    Policy("controlled, no staleness check", check_staleness=False),
    Policy("controlled, no evidence gate", evidence_gate=False),
)


@dataclass
class Findings:
    service: str
    metrics: dict | None
    logs: list[str] | None
    deploys: list[dict] | None
    problems: dict[str, str]  # tool -> why it has no data
    metric_causes: set[str]
    log_causes: Counter
    hop: str | None


@dataclass
class Report:
    incident_id: str
    outcome: str  # diagnosed | escalated
    service: str | None
    cause: str | None
    evidence: list[str]
    missing: list[str]
    visited: list[str]
    tool_calls: int
    failed_calls: int
    elapsed_ms: int
    spans: list[dict] = field(default_factory=list, repr=False)

    def summary(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "spans"}


class Investigator:
    def __init__(self, policy: Policy, session: Session) -> None:
        self.policy, self.session = policy, session

    def fetch(self, tool: str, service: str):
        """Returns (data, None) or (None, reason). Retries according to the policy."""
        p, attempt = self.policy, 0
        while True:
            if self.session.budget_left <= 0:
                return None, "call budget exhausted"
            attempt += 1
            r = self.session.call(tool, service)
            if r.status == "ok":
                stale = tool == "metrics" and r.data["as_of_ms"] < self.session.clock_ms - STALE_AFTER_MS
                if stale and p.check_staleness:
                    self.session.note(f"{tool}({service}) is stale (as_of {r.data['as_of_ms']} ms), re-reading")
                    if attempt < p.max_attempts:
                        continue
                    return None, "stale"
                return r.data, None
            if p.retry == "none" or attempt >= p.max_attempts:
                return None, r.status
            if p.retry == "classified":
                if r.status == "unavailable":
                    return None, r.status  # not retryable: stop spending calls on it
                wait = r.retry_after_ms or BACKOFF_MS * 2 ** (attempt - 1)
                self.session.wait(wait, f"{r.status} on {tool}({service})")

    def examine(self, service: str) -> Findings:
        problems = {}
        metrics, problem = self.fetch("metrics", service)
        if problem:
            problems["metrics"] = problem
        logs, problem = self.fetch("logs", service)
        if problem:
            problems["logs"] = problem
        deploys, problem = self.fetch("deploys", service)
        if problem:
            problems["deploys"] = problem
        recent = any(d["minutes_before_alert"] <= RECENT_DEPLOY_MINUTES for d in deploys or [])
        return Findings(
            service,
            metrics,
            logs,
            deploys,
            problems,
            metric_signals(metrics, recent) if metrics else set(),
            log_signals(logs) if logs else Counter(),
            next_hop(logs) if logs else None,
        )

    def own_cause(self, f: Findings) -> str | None:
        """The cause this service shows by itself, under the policy's standard of evidence."""
        confirmed = [c for c in f.log_causes if c in f.metric_causes]
        if confirmed:
            return max(confirmed, key=lambda c: (f.log_causes[c], c))
        if self.policy.evidence_gate:
            return None
        if f.log_causes:
            return max(f.log_causes, key=lambda c: (f.log_causes[c], c))
        return min(f.metric_causes) if f.metric_causes else None

    def fallback_hop(self, service: str) -> str | None:
        """Without logs, look for the dependency whose own metrics look wrong."""
        deps, _ = self.fetch("dependencies", service)
        suspects = []
        for dep in deps or []:
            m, _ = self.fetch("metrics", dep)
            if m and (metric_signals(m, recent_deploy=False) or m["error_rate"] > 0.05):
                suspects.append((m["error_rate"], dep))
        return max(suspects)[1] if suspects else None

    def run(self, incident_id: str, alert: str) -> Report:
        s, visited = alert, []
        self.session.alert(alert)
        while True:
            visited.append(s)
            f = self.examine(s)
            cause = self.own_cause(f)
            if cause:
                return self._report(incident_id, "diagnosed", s, cause, f, visited)
            hop = f.hop
            if hop is None and f.logs is None and self.policy.descend:
                hop = self.fallback_hop(s)
            if hop and not self.policy.descend:
                self.session.note(f"errors point at {hop}; single pass blames it without looking")
                return self._report(incident_id, "diagnosed", hop, None, f, visited)
            if hop and hop not in visited:
                self.session.note(f"{s} is a victim; errors point at {hop}")
                s = hop
                continue
            guess = self._best_guess(f)
            if self.policy.evidence_gate:
                return self._report(incident_id, "escalated", s, guess, f, visited)
            return self._report(incident_id, "diagnosed", s, guess, f, visited)

    @staticmethod
    def _best_guess(f: Findings) -> str:
        if f.log_causes:
            return max(f.log_causes, key=lambda c: (f.log_causes[c], c))
        if f.metric_causes:
            return min(f.metric_causes)
        return CAUSES[kind(f.service)][0]

    def _report(self, incident_id: str, outcome: str, service: str, cause: str | None, f: Findings, visited) -> Report:
        evidence = []
        if cause and f.metrics and cause in f.metric_causes:
            key = METRIC_TESTS[cause][0]
            evidence.append(f"metrics({f.service}): {key}={f.metrics[key]}")
        if cause and f.log_causes.get(cause):
            evidence.append(f"logs({f.service}): '{LOG_SIGNATURES[cause]}' x{f.log_causes[cause]}")
        if cause == "bad_deploy" and f.deploys:
            last = min(f.deploys, key=lambda d: d["minutes_before_alert"])
            evidence.append(f"deploys({f.service}): {last['version']} {last['minutes_before_alert']} min before alert")
        missing = [f"{tool}({f.service}): {why}" for tool, why in sorted(f.problems.items())]
        spans = self.session.spans
        self.session.note(f"{outcome}: {cause or 'unknown cause'} on {service}")
        return Report(
            incident_id,
            outcome,
            service,
            cause,
            evidence,
            missing,
            list(visited),
            self.session.tool_calls,
            sum(s["kind"] == "tool" and s["status"] != "ok" for s in spans),
            self.session.clock_ms,
            spans,
        )
