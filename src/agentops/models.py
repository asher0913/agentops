from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"


class Phase(StrEnum):
    PLAN = "plan"
    EXECUTE = "execute"
    VALIDATE = "validate"
    RECOVER = "recover"
    REPORT = "report"


@dataclass(frozen=True)
class Incident:
    id: str
    service: str
    summary: str
    severity: Severity = Severity.SEV2


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, str]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    evidence: dict[str, float | str] = field(default_factory=dict)
    retryable: bool = False


@dataclass(frozen=True)
class TraceEvent:
    phase: Phase
    message: str
    tool_name: str | None = None
    attempt: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class IncidentReport:
    incident_id: str
    status: str
    probable_cause: str
    evidence: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    trace: tuple[TraceEvent, ...]
    recovered_tool_failures: int
    total_tool_calls: int

