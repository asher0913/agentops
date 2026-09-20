from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import Incident, ToolResult


class AgentTool(Protocol):
    name: str

    async def execute(self, incident: Incident, arguments: dict[str, str]) -> ToolResult: ...


class RunbookTool:
    name = "search_runbook"

    def __init__(self, runbooks: dict[str, str] | None = None) -> None:
        self.runbooks = runbooks or {
            "checkout": (
                "Check DB pool saturation, cache hit rate, then downstream payment latency."
            ),
            "catalog": "Check search cluster health and product cache freshness.",
        }

    async def execute(self, incident: Incident, arguments: dict[str, str]) -> ToolResult:
        content = self.runbooks.get(incident.service)
        return ToolResult(
            ok=content is not None,
            content=content or "No matching runbook.",
            retryable=False,
        )


class MetricsTool:
    name = "query_metrics"

    def __init__(self, snapshots: dict[str, dict[str, float]] | None = None) -> None:
        self.snapshots = snapshots or {
            "checkout": {"p95_ms": 1280.0, "db_pool_usage": 0.97, "error_rate": 0.08},
            "catalog": {"p95_ms": 220.0, "cache_hit_rate": 0.93, "error_rate": 0.002},
        }

    async def execute(self, incident: Incident, arguments: dict[str, str]) -> ToolResult:
        values = self.snapshots.get(incident.service)
        if values is None:
            return ToolResult(False, "Metrics unavailable.", retryable=True)
        summary = ", ".join(f"{key}={value}" for key, value in values.items())
        return ToolResult(True, summary, evidence=values)


class LogsTool:
    name = "search_logs"

    def __init__(self, logs: dict[str, list[str]] | None = None) -> None:
        self.logs = logs or {
            "checkout": [
                "Timeout acquiring database connection",
                "Pool wait exceeded 900ms",
            ],
            "catalog": ["Request completed status=200"],
        }

    async def execute(self, incident: Incident, arguments: dict[str, str]) -> ToolResult:
        entries = self.logs.get(incident.service, [])
        return ToolResult(bool(entries), " | ".join(entries) or "No logs found.")


class FaultOnceTool:
    """Test/demo wrapper that makes recovery behavior deterministic."""

    def __init__(self, wrapped: AgentTool) -> None:
        self.wrapped = wrapped
        self.name = wrapped.name
        self.failed = False

    async def execute(self, incident: Incident, arguments: dict[str, str]) -> ToolResult:
        if not self.failed:
            self.failed = True
            return ToolResult(False, "Injected transient failure.", retryable=True)
        return await self.wrapped.execute(incident, arguments)


ToolFactory = Callable[[], AgentTool]
