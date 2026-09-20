import pytest

from agentops.agent import IncidentAgent, evaluate_reports
from agentops.models import Incident, Phase
from agentops.tools import FaultOnceTool, LogsTool, MetricsTool, RunbookTool


@pytest.mark.asyncio
async def test_agent_recovers_transient_tool_failure() -> None:
    agent = IncidentAgent(
        [RunbookTool(), FaultOnceTool(MetricsTool()), LogsTool()],
        max_retries=2,
    )
    report = await agent.run(Incident("inc-1", "checkout", "Latency increased"))
    assert report.probable_cause == "Database connection pool saturation"
    assert report.recovered_tool_failures == 1
    assert any(event.phase == Phase.RECOVER for event in report.trace)
    assert evaluate_reports([report])["task_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_agent_escalates_when_evidence_is_missing() -> None:
    agent = IncidentAgent([], max_retries=0)
    agent.tools = {}
    report = await agent.run(Incident("inc-2", "unknown", "Unknown failure"))
    assert report.status == "needs_attention"
    assert report.probable_cause == "Insufficient evidence"

