"""A seeded microservice system: injected root causes and the telemetry they leave behind.

A fault at one service shows up as latency and errors in every service that
calls it, and the alert usually fires upstream of the fault. Error logs name
the dependency that failed, which is the trail an investigator follows down.
Incidents also carry the distractions real ones do: a harmless recent deploy
on a service in the path, CPU throttling in callers that are retrying, and a
few log lines left over from an unrelated old problem.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

SERVICES = {  # name -> (kind, dependencies); the call graph is a tree rooted at the frontend
    "web-frontend": ("api", ("checkout-api", "catalog-api", "auth-svc")),
    "checkout-api": ("api", ("payments-api", "cart-cache", "orders-db")),
    "catalog-api": ("api", ("search-index", "catalog-db")),
    "payments-api": ("api", ("payments-db", "fraud-svc")),
    "fraud-svc": ("api", ("feature-store",)),
    "auth-svc": ("api", ("session-store",)),
    "orders-db": ("db", ()),
    "payments-db": ("db", ()),
    "catalog-db": ("db", ()),
    "feature-store": ("db", ()),
    "cart-cache": ("cache", ()),
    "session-store": ("cache", ()),
    "search-index": ("index", ()),
}

CAUSES = {
    "api": ("bad_deploy", "memory_leak", "cpu_throttling", "cert_expiry"),
    "db": ("connection_exhaustion", "disk_full", "cpu_throttling"),
    "cache": ("eviction_storm", "cpu_throttling"),
    "index": ("disk_full", "cpu_throttling"),
}

LOG_SIGNATURES = {
    "bad_deploy": "unhandled exception in request handler",
    "memory_leak": "container OOMKilled, restarting",
    "cpu_throttling": "request exceeded 1s deadline",
    "cert_expiry": "tls handshake failed: certificate has expired",
    "connection_exhaustion": "too many connections",
    "disk_full": "write failed: no space left on device",
    "eviction_storm": "evicted keys under memory pressure",
}
PROPAGATED = ("timeout calling {dep}", "upstream error from {dep}")
BENIGN = ("GET /health 200", "request completed status=200", "config reloaded", "GET /metrics 200")
LATENCY_MS = {"api": 120.0, "db": 9.0, "cache": 1.5, "index": 35.0}
STALE_AGE_MS = 15 * 60 * 1000


def kind(service: str) -> str:
    return SERVICES[service][0]


def dependencies(service: str) -> tuple[str, ...]:
    return SERVICES[service][1]


def caller(service: str) -> str | None:
    return next((s for s, (_, deps) in SERVICES.items() if service in deps), None)


def path_from(alert: str, root: str) -> list[str]:
    """Services from the alerting one down to the root cause (inclusive)."""
    chain = [root]
    while chain[-1] != alert:
        up = caller(chain[-1])
        if up is None:
            raise ValueError(f"{alert} does not depend on {root}")
        chain.append(up)
    return chain[::-1]


@dataclass
class Incident:
    id: str
    root: str
    cause: str
    alert: str
    metrics: dict[str, dict[str, float]]  # current, during the incident
    baseline: dict[str, dict[str, float]]  # before the incident; what a stale read returns
    logs: dict[str, list[str]]
    deploys: dict[str, list[dict]]
    unavailable: set[tuple[str, str]] = field(default_factory=set)  # (tool, service) down for the incident

    @property
    def path(self) -> list[str]:
        return path_from(self.alert, self.root)

    @property
    def root_evidence_missing(self) -> bool:
        return bool({("metrics", self.root), ("logs", self.root)} & self.unavailable)


def _baseline(service: str, rng: random.Random) -> dict[str, float]:
    k = kind(service)
    m = {
        "latency_p95_ms": round(LATENCY_MS[k] * rng.uniform(0.85, 1.15), 2),
        "error_rate": round(rng.uniform(0.0005, 0.004), 4),
        "cpu_throttle": round(rng.uniform(0.0, 0.12), 3),
    }
    if k == "api":
        m |= {"memory_pct": round(rng.uniform(35, 75), 1), "restarts": 0, "tls_errors_per_min": rng.randint(0, 3)}
    elif k == "db":
        m |= {"connections_pct": round(rng.uniform(25, 75), 1), "disk_pct": round(rng.uniform(35, 85), 1)}
    elif k == "cache":
        m |= {"hit_rate": round(rng.uniform(0.9, 0.99), 3), "memory_pct": round(rng.uniform(40, 80), 1)}
    else:
        m |= {"disk_pct": round(rng.uniform(35, 85), 1)}
    return m


def _inject(cause: str, m: dict[str, float], rng: random.Random) -> None:
    def scale_latency(lo: float, hi: float) -> None:
        m["latency_p95_ms"] = round(m["latency_p95_ms"] * rng.uniform(lo, hi), 2)

    if cause == "bad_deploy":
        m["error_rate"] = round(rng.uniform(0.08, 0.35), 4)
        scale_latency(1.2, 2.0)
    elif cause == "memory_leak":
        m |= {"memory_pct": round(rng.uniform(92, 99), 1), "restarts": rng.randint(2, 6)}
        m["error_rate"] = round(rng.uniform(0.02, 0.1), 4)
        scale_latency(1.5, 3.0)
    elif cause == "cpu_throttling":
        m["cpu_throttle"] = round(rng.uniform(0.4, 0.85), 3)
        scale_latency(3.0, 6.0)
    elif cause == "cert_expiry":
        m["tls_errors_per_min"] = rng.randint(40, 400)
    elif cause == "connection_exhaustion":
        m["connections_pct"] = round(rng.uniform(97, 100), 1)
        m["error_rate"] = round(rng.uniform(0.05, 0.2), 4)
        scale_latency(4.0, 10.0)
    elif cause == "disk_full":
        m["disk_pct"] = round(rng.uniform(96, 100), 1)
        m["error_rate"] = round(rng.uniform(0.1, 0.5), 4)
    elif cause == "eviction_storm":
        m["hit_rate"] = round(rng.uniform(0.2, 0.55), 3)
        scale_latency(2.0, 4.0)


def generate(seed: int, n: int, unavailable_rate: float = 0.04) -> list[Incident]:
    incidents = []
    names = list(SERVICES)
    for i in range(n):
        rng = random.Random(f"world:{seed}:{i}")
        root = rng.choice(names)
        cause = rng.choice(CAUSES[kind(root)])
        above = []
        s = caller(root)
        while s is not None:
            above.append(s)
            s = caller(s)
        alert = rng.choice([root, *above])
        path = path_from(alert, root)

        baseline = {s: _baseline(s, rng) for s in names}
        metrics = {s: dict(m) for s, m in baseline.items()}
        logs = {s: [rng.choice(BENIGN) for _ in range(rng.randint(3, 6))] for s in names}
        deploys = {
            s: [{"version": f"v{rng.randint(10, 99)}", "minutes_before_alert": rng.randint(180, 5000)}]
            if rng.random() < 0.5
            else []
            for s in names
        }

        _inject(cause, metrics[root], rng)
        logs[root] += [LOG_SIGNATURES[cause]] * rng.randint(4, 9)
        if cause == "bad_deploy":
            deploys[root].append({"version": f"v{rng.randint(100, 199)}", "minutes_before_alert": rng.randint(5, 25)})

        # symptoms travel up to every caller, weaker with distance
        child, s, distance = root, caller(root), 1
        while s is not None:
            m = metrics[s]
            m["latency_p95_ms"] = round(m["latency_p95_ms"] * (1 + rng.uniform(1, 3) / distance), 2)
            m["error_rate"] = round(m["error_rate"] + rng.uniform(0.02, 0.15) / distance, 4)
            if rng.random() < 0.3:  # callers retrying the failing dependency burn CPU
                m["cpu_throttle"] = round(rng.uniform(0.32, 0.5), 3)
            logs[s] += [rng.choice(PROPAGATED).format(dep=child) for _ in range(rng.randint(3, 8))]
            child, s, distance = s, caller(s), distance + 1

        if len(path) > 1 and rng.random() < 0.4:  # a harmless recent deploy somewhere above the root
            decoy = rng.choice(path[:-1])
            deploys[decoy].append({"version": f"v{rng.randint(100, 199)}", "minutes_before_alert": rng.randint(5, 40)})
        for s in names:  # leftovers from an unrelated old problem
            if rng.random() < 0.25:
                other = rng.choice([c for c in LOG_SIGNATURES if c != cause])
                logs[s] += [LOG_SIGNATURES[other]] * rng.randint(1, 3)
            rng.shuffle(logs[s])

        unavailable = {(tool, s) for s in names for tool in ("metrics", "logs") if rng.random() < unavailable_rate}
        incidents.append(
            Incident(f"inc-{seed}-{i:04d}", root, cause, alert, metrics, baseline, logs, deploys, unavailable)
        )
    return incidents
