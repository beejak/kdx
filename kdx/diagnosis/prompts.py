import json

from kdx.collector.types import DiagnosisContext

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer specializing in Kubernetes failure diagnosis.
You will receive a JSON snapshot of a failing Kubernetes Deployment.
Identify the root cause and provide one immediately actionable fix.

Rules:
- Cite specific evidence: pod names, timestamps, exit codes, log lines.
- fix_command must be a complete, copy-pasteable kubectl or YAML patch.
- root_cause is 1-2 sentences maximum. Full reasoning goes in evidence[].
- confidence is "high" only when the failure class is unambiguous from the evidence.
- Output ONLY a valid JSON object. No markdown, no explanation outside the JSON.

Schema:
{
  "failure_class": "CrashLoopBackOff|OOMKilled|ImagePullBackOff|Pending|Unknown",
  "root_cause": "string",
  "evidence": ["[source] detail", ...],
  "fix_command": "string",
  "fix_explanation": "string",
  "confidence": "high|medium|low"
}
"""

RETRY_SYSTEM_PROMPT = """Output ONLY a valid JSON object. No markdown. No explanation. No prose.
Start with { and end with }. Nothing before or after.

Required schema:
{
  "failure_class": "CrashLoopBackOff|OOMKilled|ImagePullBackOff|Pending|Unknown",
  "root_cause": "string",
  "evidence": ["string"],
  "fix_command": "string",
  "fix_explanation": "string",
  "confidence": "high|medium|low"
}
"""


def _truncate_log_field(value: str | None) -> str | None:
    if value is None:
        return None
    lines = value.splitlines()
    if len(lines) <= 60:
        return value
    omitted = len(lines) - 60
    return "\n".join(
        lines[:10] + [f"[... {omitted} lines omitted ...]"] + lines[-50:],
    )


def build_context_message(ctx: DiagnosisContext) -> str:
    prefix = f"PRE-CLASSIFICATION: {ctx.failure_class}\n\n"
    data = json.loads(ctx.model_dump_json(indent=2))
    for pod in data.get("pods", []):
        if "logs" in pod:
            pod["logs"] = _truncate_log_field(pod["logs"])
        if "previous_logs" in pod:
            pod["previous_logs"] = _truncate_log_field(pod["previous_logs"])
    body = json.dumps(data, indent=2)
    out = prefix + body
    if len(out) > 60000:
        ne = data.get("namespace_events", [])
        data["namespace_events"] = ne[:20]
        body = json.dumps(data, indent=2)
        out = prefix + body
    if len(out) > 60000:
        for pod in data.get("pods", []):
            pod["previous_logs"] = None
        body = json.dumps(data, indent=2)
        out = prefix + body
    if len(out) > 60000:
        for pod in data.get("pods", []):
            logs = pod.get("logs")
            if logs:
                lines = logs.splitlines()
                pod["logs"] = "\n".join(lines[-30:])
        body = json.dumps(data, indent=2)
        out = prefix + body
    return out
