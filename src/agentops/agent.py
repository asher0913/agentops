from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .models import Incident, IncidentReport, Phase, ToolCall, ToolResult, TraceEvent
from .tools import AgentTool, LogsTool, MetricsTool, RunbookTool


class IncidentAgent:
    """A deterministic state machine around tool calls.

    An LLM can replace ``plan`` and ``synthesize``; control flow, retries, policy,
    tracing and evaluation stay deterministic and testable.
    """

    def __init__(self, tools: Iterable[AgentTool] | None = None, max_retries: int = 2) -> None:
        provided = list(tools or [RunbookTool(), MetricsTool(), LogsTool()])
        self.tools = {tool.name: tool for tool in provided}
        self.max_retries = max_retries

    def plan(self, incident: Incident) -> list[ToolCall]:
        return [
            ToolCall("search_runbook", {"query": incident.summary}),
            ToolCall("query_metrics", {"window": "15m"}),
            ToolCall("search_logs", {"window": "15m", "query": incident.summary}),
        ]

    async def run(self, incident: Incident) -> IncidentReport:
        trace = [TraceEvent(Phase.PLAN, f"Created plan for {incident.service}")]
        evidence: list[str] = []
        results: dict[str, ToolResult] = {}
        recovered = 0
        total_calls = 0
        for call in self.plan(incident):
            tool = self.tools.get(call.name)
            if tool is None:
                trace.append(TraceEvent(Phase.VALIDATE, "Tool not registered", call.name))
                continue
            for attempt in range(1, self.max_retries + 2):
                total_calls += 1
                trace.append(
                    TraceEvent(Phase.EXECUTE, "Calling tool", call.name, attempt=attempt)
                )
                result = await tool.execute(incident, call.arguments)
                if result.ok:
                    if attempt > 1:
                        recovered += 1
                        trace.append(
                            TraceEvent(
                                Phase.RECOVER,
                                "Recovered transient failure",
                                call.name,
                                attempt,
                            )
                        )
                    results[call.name] = result
                    evidence.append(f"{call.name}: {result.content}")
                    break
                trace.append(
                    TraceEvent(Phase.VALIDATE, result.content, call.name, attempt=attempt)
                )
                if not result.retryable or attempt > self.max_retries:
                    results[call.name] = result
                    break
                await asyncio.sleep(0)
        cause, actions, status = self.synthesize(results)
        trace.append(TraceEvent(Phase.REPORT, f"Incident classified as {status}"))
        return IncidentReport(
            incident_id=incident.id,
            status=status,
            probable_cause=cause,
            evidence=tuple(evidence),
            recommended_actions=tuple(actions),
            trace=tuple(trace),
            recovered_tool_failures=recovered,
            total_tool_calls=total_calls,
        )

    @staticmethod
    def synthesize(results: dict[str, ToolResult]) -> tuple[str, list[str], str]:
        metrics = results.get("query_metrics")
        logs = results.get("search_logs")
        if metrics and metrics.ok and float(metrics.evidence.get("db_pool_usage", 0)) > 0.9:
            return (
                "Database connection pool saturation",
                [
                    "Inspect long-running queries and connection leaks.",
                    "Temporarily increase pool capacity within the database safety limit.",
                    "Add an alert for pool wait time before user latency breaches the SLO.",
                ],
                "action_required",
            )
        if logs and "Timeout" in logs.content:
            return (
                "Downstream timeout",
                ["Check dependency health and retry budget."],
                "action_required",
            )
        if metrics and metrics.ok:
            return "No critical anomaly found", ["Continue monitoring."], "stable"
        return (
            "Insufficient evidence",
            ["Escalate with trace ID and missing tool data."],
            "needs_attention",
        )


def evaluate_reports(reports: list[IncidentReport]) -> dict[str, float]:
    if not reports:
        return {"task_success_rate": 0.0, "recovery_rate": 0.0, "avg_tool_calls": 0.0}
    success = sum(report.probable_cause != "Insufficient evidence" for report in reports)
    recovered = sum(report.recovered_tool_failures for report in reports)
    retry_opportunities = sum(report.total_tool_calls > 3 for report in reports)
    return {
        "task_success_rate": success / len(reports),
        "recovery_rate": recovered / max(retry_opportunities, 1),
        "avg_tool_calls": sum(report.total_tool_calls for report in reports) / len(reports),
    }
