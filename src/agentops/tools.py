"""Telemetry tools with realistic failure modes, a traced session, and record/replay.

Every tool call goes through a :class:`Session`, which keeps a simulated clock
and appends one span per call (plus waits and agent decisions). A recorded
trace can be replayed: :class:`ReplayBackend` serves the recorded responses in
order and raises :class:`ReplayDivergence` the moment an agent asks for a call
the recording does not contain, which turns a folder of production traces into
an offline regression suite for agent changes.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass

from .world import STALE_AGE_MS, Incident, dependencies

TOOLS = ("metrics", "logs", "deploys", "dependencies")
BASE_LATENCY_MS = {"metrics": 120, "logs": 350, "deploys": 80, "dependencies": 40}
TIMEOUT_MS = 2000
RATE_LIMIT_WINDOW_MS = 1000


@dataclass(frozen=True)
class Response:
    status: str  # ok | transient | timeout | rate_limited | unavailable
    data: object = None
    latency_ms: int = 0
    retry_after_ms: int = 0


class LiveBackend:
    """Reads the incident's telemetry through a flaky observability stack.

    Per call, with total probability ``fault_rate``: a transient error (50%),
    a timeout that costs two seconds (20%), or a rate limit that rejects every
    call to that tool until the window passes (30%). Metric reads are
    additionally stale with probability ``fault_rate / 2``: they succeed but
    return the pre-incident snapshot with an old ``as_of``. Some (tool, service)
    pairs are down for the whole incident and fail with ``unavailable``.

    Fault draws are keyed by (incident, tool, service, n-th call), so every
    agent that makes the same calls sees the same faults.
    """

    def __init__(self, incident: Incident, fault_rate: float = 0.2, seed: int = 0) -> None:
        self.incident, self.fault_rate, self.seed = incident, fault_rate, seed
        self.calls: Counter[tuple[str, str]] = Counter()
        self.blocked_until: dict[str, int] = {}

    def call(self, tool: str, service: str, now_ms: int) -> Response:
        n = self.calls[(tool, service)]
        self.calls[(tool, service)] += 1
        rng = random.Random(f"fault:{self.seed}:{self.incident.id}:{tool}:{service}:{n}")
        base = BASE_LATENCY_MS[tool]
        if (tool, service) in self.incident.unavailable:
            return Response("unavailable", latency_ms=base)
        if now_ms < self.blocked_until.get(tool, -1):
            return Response("rate_limited", latency_ms=5, retry_after_ms=self.blocked_until[tool] - now_ms)
        roll, f = rng.random(), self.fault_rate
        if roll < 0.5 * f:
            return Response("transient", latency_ms=base // 2)
        if roll < 0.7 * f:
            return Response("timeout", latency_ms=TIMEOUT_MS)
        if roll < f:
            self.blocked_until[tool] = now_ms + RATE_LIMIT_WINDOW_MS
            return Response("rate_limited", latency_ms=5, retry_after_ms=RATE_LIMIT_WINDOW_MS)
        stale = tool == "metrics" and rng.random() < 0.5 * f
        return Response("ok", self._read(tool, service, now_ms, stale), int(base * rng.uniform(0.8, 1.5)))

    def _read(self, tool: str, service: str, now_ms: int, stale: bool) -> object:
        inc = self.incident
        if tool == "metrics":
            values = inc.baseline[service] if stale else inc.metrics[service]
            return {**values, "as_of_ms": now_ms - STALE_AGE_MS if stale else now_ms}
        if tool == "logs":
            return list(inc.logs[service])
        if tool == "deploys":
            return [dict(d) for d in inc.deploys[service]]
        return list(dependencies(service))


class ReplayDivergence(Exception):
    """The agent asked for a call that the recorded trace does not contain at this point."""


class ReplayBackend:
    def __init__(self, spans: list[dict]) -> None:
        self.calls = [s for s in spans if s["kind"] == "tool"]
        self.position = 0

    def call(self, tool: str, service: str, now_ms: int) -> Response:
        if self.position >= len(self.calls):
            raise ReplayDivergence(f"call {self.position}: {tool}({service}) is beyond the recording")
        span = self.calls[self.position]
        if (span["tool"], span["service"]) != (tool, service):
            raise ReplayDivergence(
                f"call {self.position}: agent asked for {tool}({service}), "
                f"recording has {span['tool']}({span['service']})"
            )
        self.position += 1
        return Response(span["status"], span["response"], span["latency_ms"], span["retry_after_ms"])


class Session:
    """One incident investigation: a simulated clock and an append-only span log."""

    def __init__(self, backend, trace_id: str, call_budget: int = 30) -> None:
        self.backend, self.trace_id, self.call_budget = backend, trace_id, call_budget
        self.clock_ms = 0
        self.spans: list[dict] = []
        self.attempts: Counter[tuple[str, str]] = Counter()

    @property
    def tool_calls(self) -> int:
        return sum(s["kind"] == "tool" for s in self.spans)

    @property
    def budget_left(self) -> int:
        return self.call_budget - self.tool_calls

    def call(self, tool: str, service: str) -> Response:
        self.attempts[(tool, service)] += 1
        r = self.backend.call(tool, service, self.clock_ms)
        self.spans.append(
            {
                "trace_id": self.trace_id,
                "span_id": len(self.spans),
                "kind": "tool",
                "t_ms": self.clock_ms,
                "tool": tool,
                "service": service,
                "attempt": self.attempts[(tool, service)],
                "status": r.status,
                "latency_ms": r.latency_ms,
                "retry_after_ms": r.retry_after_ms,
                "response": r.data,
            }
        )
        self.clock_ms += r.latency_ms
        return r

    def alert(self, service: str) -> None:
        self.spans.append(
            {"trace_id": self.trace_id, "span_id": len(self.spans), "kind": "alert", "t_ms": self.clock_ms,
             "service": service}
        )  # fmt: skip

    def wait(self, ms: int, reason: str) -> None:
        self.spans.append(
            {"trace_id": self.trace_id, "span_id": len(self.spans), "kind": "wait", "t_ms": self.clock_ms, "ms": ms,
             "reason": reason}
        )  # fmt: skip
        self.clock_ms += ms

    def note(self, message: str) -> None:
        self.spans.append(
            {"trace_id": self.trace_id, "span_id": len(self.spans), "kind": "decision", "t_ms": self.clock_ms,
             "message": message}
        )  # fmt: skip


def dump_trace(spans: list[dict], path) -> None:
    with open(path, "w") as fh:
        for span in spans:
            fh.write(json.dumps(span, sort_keys=True) + "\n")


def load_trace(path) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]
