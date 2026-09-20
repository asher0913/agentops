# AgentOps

[![CI](https://github.com/asher0913/agentops/actions/workflows/ci.yml/badge.svg)](https://github.com/asher0913/agentops/actions/workflows/ci.yml)

An observable incident-response agent that treats reliability as deterministic control flow rather
than an LLM prompt.

```text
plan -> execute tools -> validate evidence -> recover retryable failures -> report or escalate
```

## Highlights

- Typed log, metric and runbook tools.
- Explicit state machine with bounded retry budgets.
- Retryable/non-retryable failure classification.
- Deterministic fault injection for recovery tests.
- Replayable traces with phase, tool, attempt and timestamp.
- Evidence-based diagnosis and automatic escalation when evidence is incomplete.
- Metrics for task success, recovery rate and tool-call efficiency.

An LLM can replace `plan` and `synthesize`; registration, retries, evidence checks and escalation
remain deterministic and testable.

## Quick start

```bash
uv sync --extra dev
uv run pytest
uv run agentops-demo
uv run uvicorn agentops.api:app --reload
```

Open `http://127.0.0.1:8000/docs` for the API explorer.

## Example response

```json
{
  "status": "action_required",
  "probable_cause": "Database connection pool saturation",
  "recovered_tool_failures": 1,
  "recommended_actions": [
    "Inspect long-running queries and connection leaks.",
    "Add an alert for pool wait time before the SLO is breached."
  ]
}
```

## Production roadmap

- Persist checkpoints and add idempotency keys.
- Require approvals for mutating remediation tools.
- Redact secrets and PII from traces.
- Replay historical incidents as an agent regression suite.
- Add OpenTelemetry spans and SLO-based evaluation.

## License

MIT

