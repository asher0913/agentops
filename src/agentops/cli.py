from __future__ import annotations

import asyncio
import json

from .agent import IncidentAgent, evaluate_reports
from .models import Incident
from .tools import FaultOnceTool, LogsTool, MetricsTool, RunbookTool


async def _demo() -> None:
    agent = IncidentAgent(
        tools=[RunbookTool(), FaultOnceTool(MetricsTool()), LogsTool()],
        max_retries=2,
    )
    report = await agent.run(
        Incident("inc-1001", "checkout", "Checkout P95 and error rate increased")
    )
    print(json.dumps({
        "status": report.status,
        "probable_cause": report.probable_cause,
        "actions": report.recommended_actions,
        "recovered_tool_failures": report.recovered_tool_failures,
        "metrics": evaluate_reports([report]),
    }, indent=2))


def main() -> None:
    asyncio.run(_demo())

