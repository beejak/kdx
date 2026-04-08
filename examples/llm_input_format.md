# What kdx sends to the LLM

Command-line reference: `kdx --help`, `kdx diagnose --help`.

Each diagnosis is **one chat request** with two parts:

| Role | Content |
|------|---------|
| **system** | Fixed instructions: JSON-only reply, schema for `DiagnosisResult`, SRE rules. First attempt uses the long prompt; if parsing fails, the second attempt uses a shorter retry prompt (`RETRY_SYSTEM_PROMPT` in `kdx/diagnosis/prompts.py`). |
| **user** | A single text blob built by `build_context_message(ctx)` in `kdx/diagnosis/prompts.py`. |

There is **no separate file upload** in the app: the “payload” is that **user** string. The same structure is what you get from `kdx diagnose ... --dump-context path.json` (raw `DiagnosisContext` JSON) except the message adds a header and may trim long logs / shrink very large JSON for token limits.

**Machine-readable sample (JSON only, no `PRE-CLASSIFICATION` line):** [`diagnosis_context.sample.json`](diagnosis_context.sample.json) — same schema as `--dump-context` output and as mock fixtures under `tests/fixtures/` (content mirrors the `crash_loop` fixture, pretty-printed).

## User message format

1. First line: `PRE-CLASSIFICATION: <failure_class>` (from the collector’s `_classify_failure`, e.g. `CrashLoopBackOff`).
2. Blank line.
3. Pretty-printed JSON (2-space indent) of the full **`DiagnosisContext`**: `collected_at`, `cluster_name`, `namespace`, `deployment`, `pods` (each with `container_statuses`, `events`, `logs`, `previous_logs`, …), `namespace_events`, `failure_class`, `mock`.

If any pod `logs` / `previous_logs` exceed 60 lines, those fields are shortened (first 10 + marker + last 50 lines). If the whole user message is still over ~60k characters, `namespace_events` and logs are trimmed further (see `build_context_message`).

## Sample user message (illustrative)

Below is representative of what the **user** role contains for a crash-loop style fixture (short logs, no extra trimming):

```
PRE-CLASSIFICATION: CrashLoopBackOff

{
  "collected_at": "2024-06-01T12:00:00Z",
  "cluster_name": "docker-desktop",
  "namespace": "kdx-test",
  "deployment": {
    "name": "crash-demo",
    "namespace": "kdx-test",
    "desired_replicas": 1,
    "ready_replicas": 0,
    "available_replicas": 0,
    "conditions": [
      {
        "type": "Available",
        "status": "False"
      }
    ],
    "image": "busybox:1.36",
    "selector": {
      "app": "crash-demo"
    }
  },
  "pods": [
    {
      "pod_name": "crash-demo-abc123",
      "phase": "Running",
      "node_name": "node-1",
      "conditions": [
        {
          "type": "Ready",
          "status": "False"
        }
      ],
      "container_statuses": [
        {
          "name": "crasher",
          "ready": false,
          "restart_count": 12,
          "state": "waiting",
          "reason": "CrashLoopBackOff",
          "exit_code": null,
          "last_state_reason": "Error",
          "last_exit_code": 1
        }
      ],
      "resource_limits": {
        "crasher": {
          "cpu_request": null,
          "cpu_limit": null,
          "memory_request": "32Mi",
          "memory_limit": "64Mi"
        }
      },
      "events": [
        {
          "timestamp": "2024-06-01T11:59:00Z",
          "reason": "BackOff",
          "message": "Back-off restarting failed container crasher",
          "count": 5,
          "source_component": "kubelet"
        }
      ],
      "logs": "ERROR: cannot connect to db:5432\n",
      "previous_logs": "ERROR: cannot connect to db:5432\n"
    }
  ],
  "namespace_events": [],
  "failure_class": "CrashLoopBackOff",
  "mock": true
}
```

## Expected model reply format

The model must return **only** a JSON object (no markdown wrapper if possible) matching:

```json
{
  "failure_class": "CrashLoopBackOff|OOMKilled|ImagePullBackOff|Pending|Unknown",
  "root_cause": "string",
  "evidence": ["[source] detail", "..."],
  "fix_command": "string",
  "fix_explanation": "string",
  "confidence": "high|medium|low"
}
```

`kdx` parses that into `DiagnosisResult` (see `kdx/collector/types.py`).

## Reproduce the exact user string locally

```bash
.venv/bin/python -c "
from kdx.collector.mock import load_fixture
from kdx.diagnosis.prompts import build_context_message
print(build_context_message(load_fixture('crash_loop')))
"
```

That prints the same shape the providers send as the user message (modulo any trimming for size).
