import dataclasses
from itertools import pairwise

import pytest

from agentops import (
    POLICIES,
    Investigator,
    LiveBackend,
    ReplayDivergence,
    Session,
    compare,
    generate,
    investigate,
    replay,
    sweep,
)
from agentops.agent import RECENT_DEPLOY_MINUTES, log_signals, metric_signals, next_hop
from agentops.cli import main
from agentops.tools import load_trace
from agentops.world import path_from

CONTROLLED = next(p for p in POLICIES if p.name == "controlled")
RETRY_ALL = next(p for p in POLICIES if p.name == "retry everything")


def _recent(incident, service):
    return any(d["minutes_before_alert"] <= RECENT_DEPLOY_MINUTES for d in incident.deploys[service])


def test_generation_is_deterministic_and_paths_lead_to_the_root():
    a, b = generate(3, 50), generate(3, 50)
    assert [(i.root, i.cause, i.alert, i.logs) for i in a] == [(i.root, i.cause, i.alert, i.logs) for i in b]
    assert path_from("web-frontend", "payments-db") == ["web-frontend", "checkout-api", "payments-api", "payments-db"]
    for i in a:
        assert i.path[0] == i.alert and i.path[-1] == i.root


def test_root_shows_its_cause_in_metrics_and_logs_and_victims_point_down():
    for i in generate(0, 200):
        assert i.cause in metric_signals(i.metrics[i.root], _recent(i, i.root))
        assert i.cause in log_signals(i.logs[i.root])
        for upper, lower in pairwise(i.path):
            assert next_hop(i.logs[upper]) == lower


def test_backend_faults_are_reproducible_and_rate_limits_hold_for_the_window():
    incident = generate(0, 1)[0]
    runs = []
    for _ in range(2):
        backend = LiveBackend(incident, fault_rate=0.9, seed=1)
        runs.append([backend.call("logs", incident.alert, now_ms=t * 10).status for t in range(20)])
    assert runs[0] == runs[1]
    backend = LiveBackend(incident, fault_rate=1.0, seed=0)
    statuses = []
    while not statuses or statuses[-1] != "rate_limited":
        statuses.append(backend.call("metrics", incident.alert, now_ms=0).status)
    assert backend.call("metrics", incident.alert, now_ms=500).status == "rate_limited"
    assert backend.call("metrics", incident.alert, now_ms=999).retry_after_ms == 1


def test_unavailable_tools_are_not_retried_by_the_controlled_policy():
    incident = next(i for i in generate(0, 400) if ("metrics", i.alert) in i.unavailable)
    controlled = Investigator(CONTROLLED, Session(LiveBackend(incident, 0.0), incident.id))
    assert controlled.fetch("metrics", incident.alert) == (None, "unavailable")
    assert controlled.session.tool_calls == 1
    naive = Investigator(RETRY_ALL, Session(LiveBackend(incident, 0.0), incident.id))
    naive.fetch("metrics", incident.alert)
    assert naive.session.tool_calls == RETRY_ALL.max_attempts


def test_classified_retries_honour_retry_after_and_back_off():
    seen = set()
    for incident in generate(0, 200):
        session = Session(LiveBackend(incident, fault_rate=0.6), incident.id)
        Investigator(CONTROLLED, session).run(incident.id, incident.alert)
        for span, nxt in pairwise(session.spans):
            if span["kind"] == "tool" and nxt["kind"] == "wait":
                expected = {span["retry_after_ms"]} if span["status"] == "rate_limited" else {200, 400}
                assert nxt["ms"] in expected
                seen.add(span["status"])
    assert {"rate_limited", "transient", "timeout"} <= seen


def test_stale_metrics_are_rejected_and_re_read():
    for incident in generate(0, 300):
        session = Session(LiveBackend(incident, fault_rate=0.4, seed=0), incident.id)
        Investigator(CONTROLLED, session).run(incident.id, incident.alert)
        notes = [s["message"] for s in session.spans if s["kind"] == "decision"]
        if any("stale" in n for n in notes):
            k = next(k for k, s in enumerate(session.spans) if s["kind"] == "decision" and "stale" in s["message"])
            stale_read, reread = session.spans[k - 1], session.spans[k + 1]
            assert stale_read["tool"] == "metrics" and stale_read["status"] == "ok"
            assert (reread["tool"], reread["service"]) == ("metrics", stale_read["service"])
            return
    pytest.fail("no stale read encountered")


def test_evidence_gate_keeps_searching_past_a_victim_that_looks_guilty():
    ungated = dataclasses.replace(CONTROLLED, evidence_gate=False)
    checked = 0
    for incident in generate(1, 400):
        victim = incident.alert
        if victim == incident.root or incident.unavailable or incident.root_evidence_missing:
            continue
        signals = {
            s: (metric_signals(incident.metrics[s], _recent(incident, s)), set(log_signals(incident.logs[s])))
            for s in incident.path[:-1]
        }
        if not any(signals[victim]) or any(m & lg for m, lg in signals.values()):
            continue  # nothing misleading, or misleading in both sources (the gate's documented blind spot)
        gated = investigate(CONTROLLED, incident, fault_rate=0.0)
        loose = investigate(ungated, incident, fault_rate=0.0)
        assert (gated.outcome, gated.service, gated.cause) == ("diagnosed", incident.root, incident.cause)
        assert loose.service == victim
        checked += 1
    assert checked > 20


def test_missing_root_evidence_is_escalated_with_the_suspect_named():
    incidents = [i for i in generate(2, 400) if i.root_evidence_missing]
    reports = [investigate(CONTROLLED, i, fault_rate=0.0) for i in incidents]
    assert all(r.outcome == "escalated" for r in reports)
    assert sum(r.service == i.root for i, r in zip(incidents, reports, strict=True)) / len(reports) > 0.8
    assert all(r.missing for r in reports)


def test_replay_reproduces_a_trace_exactly_and_detects_divergence():
    incident = generate(0, 30)[7]
    original = investigate(CONTROLLED, incident, fault_rate=0.3)
    again = replay(CONTROLLED, incident.id, original.spans)
    assert again.spans == original.spans and again.summary() == original.summary()
    truncated = [s for s in original.spans if s["kind"] != "tool"] + [s for s in original.spans if s["kind"] == "tool"][
        :1
    ]
    with pytest.raises(ReplayDivergence):
        replay(CONTROLLED, incident.id, truncated)


def test_headline_ordering():
    result = compare(n=150, seeds=(0,))["policies"]
    controlled, retry_all, single = result["controlled"], result["retry everything"], result["single pass"]
    assert controlled["wrong_pct"] < 2 < 20 < retry_all["wrong_pct"] < single["wrong_pct"]
    assert controlled["correct_pct"] > retry_all["correct_pct"] > single["correct_pct"]
    assert controlled["escalations_naming_the_root_pct"] > 80


def test_naive_retries_lose_coverage_as_faults_rise():
    results = sweep((0.0, 0.2, 0.4), n=100, seeds=(0,))["policies"]["controlled, naive retries"]
    curve = [r["correct_pct"] for r in results]
    assert curve[0] > curve[1] > curve[2]


def test_cli_investigate_writes_a_replayable_trace(tmp_path, capsys):
    trace = tmp_path / "trace.jsonl"
    main(["investigate", "--incident", "5", "--trace", str(trace)])
    out = capsys.readouterr().out
    assert "alert on" in out and "ground truth" in out
    assert load_trace(trace)[0]["kind"] == "alert"
    main(["replay", str(trace)])
    assert "identical to the recording" in capsys.readouterr().out
