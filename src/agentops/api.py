from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import IncidentAgent
from .models import Incident, Severity


class IncidentRequest(BaseModel):
    id: str
    service: str
    summary: str
    severity: Severity = Severity.SEV2


def create_app(agent: IncidentAgent | None = None) -> FastAPI:
    agent = agent or IncidentAgent()
    app = FastAPI(title="AgentOps", version="0.1.0")

    @app.post("/v1/incidents")
    async def diagnose(request: IncidentRequest) -> dict[str, object]:
        report = await agent.run(Incident(**request.model_dump()))
        return {
            "incident_id": report.incident_id,
            "status": report.status,
            "probable_cause": report.probable_cause,
            "evidence": report.evidence,
            "recommended_actions": report.recommended_actions,
            "recovered_tool_failures": report.recovered_tool_failures,
            "total_tool_calls": report.total_tool_calls,
            "trace": [
                {
                    "phase": event.phase,
                    "message": event.message,
                    "tool": event.tool_name,
                    "attempt": event.attempt,
                    "created_at": event.created_at,
                }
                for event in report.trace
            ],
        }

    return app


app = create_app()

