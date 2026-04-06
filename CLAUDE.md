# kdx — Kubernetes Diagnose

## What it does

`kdx diagnose <deployment> -n <namespace>` connects to the current kubeconfig context, collects
Kubernetes signals for a failing Deployment, sends them to Claude, and prints a structured
root-cause diagnosis with a copy-pasteable fix.

Target failure classes: CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending/Unschedulable.

---

## Tech stack

- Python 3.12, Click 8, Rich 13, Pydantic v2, anthropic SDK, kubernetes Python client
- pip + pyproject.toml (no Poetry)
- pytest + pytest-mock for tests
- ruff for lint/format

---

## Project layout

```
kdx/
├── __init__.py               # __version__ = "0.1.0"
├── cli.py                    # Click entry point — thin orchestration only
├── config.py                 # Settings dataclass, env var loading
├── collector/
│   ├── types.py              # ALL shared Pydantic models (DiagnosisContext, DiagnosisResult, etc.)
│   ├── k8s.py                # Live collector — kubernetes SDK only, no subprocess
│   └── mock.py               # Loads DiagnosisContext from tests/fixtures/<name>.json
├── diagnosis/
│   ├── prompts.py            # System prompt string + build_context_message()
│   └── engine.py             # Claude API call → DiagnosisResult or raises DiagnosisError
└── output/
    └── formatter.py          # Rich terminal output

tests/
├── conftest.py               # ANTHROPIC_API_KEY=test-key dummy, shared fixtures
├── fixtures/                 # JSON files: crash_loop.json, oom_kill.json,
│   │                         #   image_pull_backoff.json, pending_unschedulable.json
├── test_collector.py
├── test_prompts.py
├── test_engine.py
└── test_formatter.py

scenarios/
├── crash_loop/deployment.yaml
├── oom_kill/deployment.yaml
├── image_pull_backoff/deployment.yaml
└── pending_unschedulable/deployment.yaml

scripts/
├── apply_scenario.sh         # kubectl apply -f scenarios/$1/ -n kdx-test (creates ns if needed)
└── reset_scenario.sh         # kubectl delete namespace kdx-test --ignore-not-found
```

---

## Import boundaries — enforced, never violated

```
cli.py → config.py, collector/*, diagnosis/*, output/*
collector/k8s.py → collector/types.py only
collector/mock.py → collector/types.py only
diagnosis/engine.py → collector/types.py, diagnosis/prompts.py
diagnosis/prompts.py → collector/types.py only
output/formatter.py → collector/types.py only
```

`diagnosis/engine.py` receives `DiagnosisContext` as a function argument.
It never imports from `collector/k8s.py` or `collector/mock.py`.
All shared types live in `collector/types.py` — that is the only cross-boundary import allowed.

---

## Data models (collector/types.py)

All models use `model_config = ConfigDict(frozen=True)`. Do not add mutable models.

```python
class K8sEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    timestamp: datetime
    reason: str           # e.g. "OOMKilling", "BackOff", "FailedScheduling"
    message: str
    count: int
    source_component: str

class ContainerStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    ready: bool
    restart_count: int
    state: str            # "running" | "waiting" | "terminated"
    reason: str | None    # "CrashLoopBackOff" | "OOMKilled" | "ErrImagePull" | etc.
    exit_code: int | None
    last_state_reason: str | None
    last_exit_code: int | None

class ResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    cpu_request: str | None
    cpu_limit: str | None
    memory_request: str | None
    memory_limit: str | None

class PodSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    pod_name: str
    phase: str            # "Pending" | "Running" | "Failed"
    node_name: str | None
    conditions: list[dict]
    container_statuses: list[ContainerStatus]
    resource_limits: dict[str, ResourceLimits]  # keyed by container name
    events: list[K8sEvent]     # pod-scoped events only
    logs: str | None           # current instance, last 100 lines; None if pod is Pending
    previous_logs: str | None  # --previous=True, last 50 lines; None if no prior instance

class DeploymentSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    namespace: str
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    conditions: list[dict]
    image: str             # first container image only
    selector: dict[str, str]  # spec.selector.matchLabels — used to find pods

class DiagnosisContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    collected_at: datetime
    cluster_name: str          # current kubeconfig context name
    namespace: str
    deployment: DeploymentSignal
    pods: list[PodSignal]      # up to 5 pods owned by the deployment
    namespace_events: list[K8sEvent]  # namespace-scoped, last 30 min, max 50
    failure_class: str         # pre-classified — see classification rules below
    mock: bool = False

class DiagnosisResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    failure_class: str         # "CrashLoopBackOff"|"OOMKilled"|"ImagePullBackOff"|"Pending"|"Unknown"
    root_cause: str            # 1-2 sentences, plain English
    evidence: list[str]        # each item prefixed with source tag: "[pod/event/log]"
    fix_command: str           # complete kubectl command or YAML block, copy-pasteable
    fix_explanation: str       # why the fix works
    confidence: str            # "high" | "medium" | "low"

class DiagnosisError(Exception):
    """Raised by engine.py for API errors, JSON parse failures, and timeouts."""
    pass
```

---

## Failure class pre-classification (_classify_failure in collector/k8s.py)

Run this before the Claude call. Priority order when multiple signals conflict:

1. **OOMKilled** — any ContainerStatus where `reason == "OOMKilled"` or `last_state_reason == "OOMKilled"`
2. **CrashLoopBackOff** — any ContainerStatus where `reason == "CrashLoopBackOff"`
3. **ImagePullBackOff** — any ContainerStatus where `reason in ("ErrImagePull", "ImagePullBackOff")`
4. **Pending** — all pods have `phase == "Pending"` and none match rules 1–3
5. **Unknown** — fallback

Return the string label. This grounds the Claude prompt in a known class and reduces hallucination.

---

## Kubernetes collector (collector/k8s.py)

### Config loading — exact order

```python
from kubernetes import client, config

def _load_kube_config(kube_context: str | None) -> str:
    try:
        config.load_kube_config(context=kube_context)
        contexts, active = config.list_kube_config_contexts()
        return active["name"]
    except config.ConfigException:
        config.load_incluster_config()
        return "in-cluster"
```

Use `load_kube_config` first (works for Docker Desktop, any kubeconfig).
Fall back to `load_incluster_config` only if kubeconfig is absent (running inside a pod).
Surface a clear error message on 403 — "kdx needs read access to Deployments, Pods, Events in namespace X. Check RBAC."

### What to collect and how

| Resource | API | Parameters |
|----------|-----|------------|
| Deployment | `AppsV1Api.read_namespaced_deployment(name, namespace)` | — |
| Pods | `CoreV1Api.list_namespaced_pod(namespace, label_selector=selector_str)` | selector from `deployment.spec.selector.match_labels`; take first 5 results |
| Pod events | `CoreV1Api.list_namespaced_event(namespace, field_selector=f"involvedObject.name={pod_name}")` | max 50, sorted by `event_time` or `last_timestamp` descending |
| Namespace events | `CoreV1Api.list_namespaced_event(namespace)` | max 50, last 30 min (`last_timestamp >= now - 30m`) |
| Pod logs | `CoreV1Api.read_namespaced_pod_log(pod_name, namespace, container=failing_container, tail_lines=100)` | skip if pod phase is Pending |
| Previous logs | same with `previous=True, tail_lines=50` | skip if restart_count == 0 |

**Label selector string**: join `deployment.spec.selector.match_labels` as `k=v,k=v` — do not use `spec.template.metadata.labels` (they may have extra labels).

**Failing container**: the first container in `pod.status.container_statuses` where `ready == False`. If all are ready, use the first container.

**Log truncation in prompts (not in collector)**: The collector stores raw logs. `prompts.py` truncates: keep first 10 lines + last 50 lines with `[... N lines omitted ...]` in between, only if total > 60 lines.

---

## Mock collector (collector/mock.py)

Fixture path resolution — must work from any working directory:

```python
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures"

def load_fixture(name: str) -> DiagnosisContext:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No fixture '{name}'. Available: {list_fixtures()}")
    return DiagnosisContext.model_validate_json(path.read_text())

def list_fixtures() -> list[str]:
    return [p.stem for p in FIXTURES_DIR.glob("*.json")]
```

---

## Diagnosis engine (diagnosis/engine.py)

```python
def diagnose(ctx: DiagnosisContext, settings: Settings) -> DiagnosisResult:
    ...
```

### Claude call

- Model: `settings.model` (default `claude-sonnet-4-5`)
- `max_tokens`: `settings.max_tokens` (default `1024`)
- Timeout: 30 seconds. Use `anthropic.Anthropic(timeout=30.0)`
- No retries in the SDK — catch `anthropic.APIStatusError` with status 529 and raise `DiagnosisError("Claude is overloaded, try again")`
- All other API exceptions → wrap in `DiagnosisError`

### Response parsing

Claude must return a JSON object. Extract it like this (handles fenced block or raw JSON):

```python
import json, re

def _extract_json(text: str) -> dict:
    # Try fenced block first
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Fall back to raw JSON
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise DiagnosisError(f"No JSON found in response: {text[:200]}")
    return json.loads(text[start:end])
```

On `json.JSONDecodeError` or `ValidationError` → raise `DiagnosisError` with the raw response truncated to 500 chars.

---

## Prompts (diagnosis/prompts.py)

### System prompt

```
You are an expert Site Reliability Engineer specializing in Kubernetes failure diagnosis.
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
```

### build_context_message(ctx: DiagnosisContext) -> str

1. Prepend: `PRE-CLASSIFICATION: {ctx.failure_class}\n\n`
2. Serialize `ctx.model_dump_json(indent=2)`
3. Apply log truncation to the JSON string: for each pod, if `logs` exceeds 60 lines keep first 10 + last 50 with `[... N lines omitted ...]`
4. Token budget: if total length > 60,000 chars, trim `namespace_events` to 20 items, then trim pod `previous_logs` to null, then trim pod `logs` to 30 lines

---

## Settings (config.py)

```python
class Settings:
    def __init__(self):
        self.anthropic_api_key: str = self._require("ANTHROPIC_API_KEY")
        self.model: str = os.getenv("KDX_MODEL", "claude-sonnet-4-5")
        self.max_tokens: int = int(os.getenv("KDX_MAX_TOKENS", "1024"))

    @staticmethod
    def _require(key: str) -> str:
        v = os.getenv(key)
        if not v:
            raise SystemExit(f"[kdx] {key} is not set. Copy .env.example to .env and fill it in.")
        return v
```

`Settings()` is constructed in `cli.py` only when the engine will be called.
For `--dump-context`-only flows or `--mock` flows, `Settings()` is still constructed — the API key is always required to avoid silent partial runs. (Product decision: if you want keyless mock runs, gate `Settings()` behind `if not mock_fixture`.)

---

## CLI (cli.py)

```
kdx diagnose DEPLOYMENT [OPTIONS]

Options:
  -n, --namespace TEXT     Kubernetes namespace [default: default]
  --mock FIXTURE           Use fixture instead of live cluster
  --dump-context PATH      Write DiagnosisContext JSON to PATH before calling Claude
  --context TEXT           Kubeconfig context name
```

Exit codes:
- `0` — success
- `1` — diagnosis error (cluster, API, parse)
- `2` — configuration error (missing env var, bad fixture name)

Use `raise SystemExit(code)` directly. Do not use `sys.exit`.

---

## Tests (tests/)

### conftest.py

```python
import os, pytest

@pytest.fixture(autouse=True)
def set_dummy_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
```

This applies to every test automatically. No test should construct `Settings()` without it.

### test_engine.py

Patch `anthropic.Anthropic.messages.create` with `pytest-mock`. Never make real API calls in tests.
Test all four fixtures. Test `DiagnosisError` is raised on bad JSON and on API 529.

### Fixture validation test (in test_collector.py)

```python
from kdx.collector.mock import list_fixtures, load_fixture

def test_all_fixtures_are_valid():
    for name in list_fixtures():
        ctx = load_fixture(name)
        assert ctx.failure_class in ("CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "Pending", "Unknown")
```

---

## pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "kdx"
version = "0.1.0"
description = "AI-powered Kubernetes deployment failure diagnosis"
requires-python = ">=3.12"
dependencies = [
    "click>=8.1",
    "rich>=13.7",
    "pydantic>=2.5",
    "anthropic>=0.25",
    "kubernetes>=29.0",
]

[project.scripts]
kdx = "kdx.cli:cli"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
    "ruff>=0.4",
    "pytest-cov>=5.0",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["kdx*"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --tb=short"

[tool.coverage.run]
source = ["kdx"]
omit = ["tests/*"]
```

---

## Scenarios (scenarios/)

### scenarios/crash_loop/deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: crash-demo
  namespace: kdx-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: crash-demo
  template:
    metadata:
      labels:
        app: crash-demo
    spec:
      containers:
        - name: crasher
          image: busybox:1.36
          command: ["sh", "-c", "echo 'ERROR: cannot connect to db:5432' >&2; exit 1"]
          resources:
            requests:
              memory: "32Mi"
            limits:
              memory: "64Mi"
```

### scenarios/oom_kill/deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oom-demo
  namespace: kdx-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oom-demo
  template:
    metadata:
      labels:
        app: oom-demo
    spec:
      containers:
        - name: memory-hog
          image: python:3.12-slim
          command: ["python3", "-c", "data=[' '*(1024*1024) for _ in range(400)]"]
          resources:
            requests:
              memory: "64Mi"
            limits:
              memory: "128Mi"
```

### scenarios/image_pull_backoff/deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: badimage-demo
  namespace: kdx-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: badimage-demo
  template:
    metadata:
      labels:
        app: badimage-demo
    spec:
      containers:
        - name: app
          image: registry.does-not-exist.invalid/myapp:v1.2.3
          resources:
            requests:
              memory: "32Mi"
```

### scenarios/pending_unschedulable/deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pending-demo
  namespace: kdx-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pending-demo
  template:
    metadata:
      labels:
        app: pending-demo
    spec:
      nodeSelector:
        disktype: ssd
      containers:
        - name: app
          image: nginx:1.25
          resources:
            requests:
              memory: "32Mi"
```

---

## Scripts

### scripts/apply_scenario.sh
```bash
#!/usr/bin/env bash
set -euo pipefail
SCENARIO=$1
kubectl create namespace kdx-test --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$(dirname "$0")/../scenarios/$SCENARIO/" --namespace kdx-test
echo "Applied $SCENARIO to kdx-test namespace"
```

### scripts/reset_scenario.sh
```bash
#!/usr/bin/env bash
set -euo pipefail
kubectl delete namespace kdx-test --ignore-not-found
echo "Deleted kdx-test namespace"
```

---

## Docker Desktop Kubernetes (one-time setup)

1. Docker Desktop → Settings → Kubernetes → Enable Kubernetes → Apply & Restart (2–3 min)
2. From WSL2: `kubectl config use-context docker-desktop && kubectl get nodes`
3. If the Python k8s client times out from WSL2, point at the Windows-side config:
   `export KUBECONFIG=/mnt/c/Users/<YourUser>/.kube/config`
4. Bootstrap: `make venv` → `make gate-phase1`

---

## Dev workflow

### No cluster (daily Cursor loop)
```bash
make venv                                  # first time only
make test                                  # all tests, mock only
.venv/bin/kdx diagnose crash-demo --mock crash_loop  # see real Rich output
```

**Always use `make <target>` or `.venv/bin/<cmd>` — never bare `pytest`, `ruff`, or `kdx`.
Cursor's terminal may resolve to Windows Python 3.9 instead of the WSL2 venv.**

### Live cluster (integration QA)
```bash
bash scripts/apply_scenario.sh crash_loop
kubectl get pods -n kdx-test -w           # wait for CrashLoopBackOff
kdx diagnose crash-demo -n kdx-test
bash scripts/reset_scenario.sh
```

### Capture a new fixture
```bash
kdx diagnose <name> -n <ns> --dump-context tests/fixtures/<name>.json
```

---

## Implementation order

Build in this order — each phase can be tested before starting the next.

1. `pyproject.toml` → `make venv` → `make gate-phase1` (`kdx --version` prints `0.1.0`)
2. `kdx/__init__.py`, `kdx/cli.py` (bare skeleton), `kdx/config.py`
3. `.env.example`, `.gitignore`
4. `kdx/collector/types.py` — all Pydantic models above, nothing else
5. `tests/fixtures/*.json` — hand-craft one per failure class matching the schema exactly
6. `kdx/collector/mock.py` — load_fixture(), list_fixtures()
7. `tests/conftest.py` + `tests/test_collector.py` — fixture validation test passes
8. `kdx/diagnosis/prompts.py` — system prompt + build_context_message()
9. `kdx/diagnosis/engine.py` — Claude call, _extract_json(), DiagnosisError
10. `tests/test_engine.py` — all four fixtures, patched SDK, error cases
11. `kdx/output/formatter.py` — Rich panels
12. Wire `kdx/cli.py` fully — all options, exit codes
13. `tests/test_formatter.py` — smoke tests
14. `kdx/collector/k8s.py` — live collector (requires Docker Desktop k8s)
15. `scenarios/` YAMLs + `scripts/` — manual QA end-to-end

---

## Cursor discipline — hard rules

These apply to every task. No exceptions, no "just this once".

1. **Do not add anything not in this spec.** No extra methods, no convenience helpers, no additional CLI flags, no config options, no logging calls beyond what's described. If you think something is missing, stop and ask.
2. **Do not add comments or docstrings to code you did not change.** Only add a comment when the logic is genuinely non-obvious. Never add docstrings as a pass.
3. **Do not add error handling for scenarios that cannot happen.** Trust Pydantic validation, trust frozen models, trust the import boundaries. Only validate at system entry points (CLI args, API responses, fixture files).
4. **Import boundaries are a hard stop.** If implementing a feature requires violating a boundary in the import rules section, stop and ask — do not work around it.
5. **Run `make gate` before marking any phase complete.** If the gate fails, fix it before moving on. Do not move to the next phase with a failing gate.
6. **Tests use mock mode only.** No test ever hits a live cluster or makes a real Claude API call. Patch the Anthropic SDK. Load fixtures via `load_fixture()`.
7. **Schema is frozen.** Do not add, rename, or remove fields from `DiagnosisContext` or `DiagnosisResult` without updating fixtures, prompts, and tests in the same commit.
8. **One concern per file.** `cli.py` orchestrates. `k8s.py` collects. `engine.py` calls Claude. `formatter.py` renders. If you find yourself putting business logic in `cli.py` or k8s calls in `engine.py`, stop.

---

## Phase gates

A phase is not complete until its gate passes. Run `make gate` — it must exit 0.

| Phase | Gate command | Passes when |
|-------|-------------|-------------|
| 1 | `make gate-phase1` | `kdx --version` prints `0.1.0` |
| 2 | `make gate` | all fixtures load, `test_all_fixtures_are_valid` passes |
| 3 | `make gate` | `test_engine.py` all pass, no real API calls |
| 4 | `make gate` | full `pytest tests/` passes, `kdx diagnose --mock crash_loop` runs |
| 5 | manual QA | all 4 scenarios diagnosed correctly against live cluster |

`make gate` = ruff check + import boundary check + pytest. All three must pass.

---

## Agent protocols

When you receive a message prefixed with one of these, follow the protocol exactly.

---

### /debug \<error or symptom\>

Use when a test is failing, a runtime error occurs, or behaviour is wrong.

1. **Read the full error.** Do not skim. Copy the exact exception type, message, and first relevant stack frame.
2. **Locate the module.** Identify which file and function the error originates from or passes through.
3. **Check boundaries first.** Run `python scripts/check_boundaries.py`. A boundary violation is often the root cause of unexpected import errors.
4. **Isolate with one targeted test.** Run `pytest -x -k <test_name> -s` — not the full suite. Use `-s` to see print output.
5. **Report before fixing.** State: file, line, root cause in one sentence, proposed fix. Do not apply the fix until the root cause is confirmed.

Do not: add debug print statements to production code, rewrite the function to avoid the issue, or change the test to make it pass without fixing the underlying problem.

---

### /gen-tests \<module path\>

Use to generate tests for a module that is missing them or has low coverage.

1. **Read the module fully** before writing a single test.
2. **List all public functions** and their signatures.
3. **For each function write:** one happy-path test + one error/edge-case test minimum.
4. **Follow existing patterns** in `tests/conftest.py` and adjacent test files. Do not introduce new pytest plugins or fixtures not already in the project.
5. **Engine tests:** always patch `anthropic.Anthropic.messages.create`. Never construct a real `Settings()` — the `autouse` fixture in `conftest.py` covers the env var.
6. **Collector tests:** always use `load_fixture()`. Never call `k8s.collect()` in a unit test.
7. **Name tests** as `test_<function>_<scenario>`, e.g. `test_load_fixture_missing_raises`.
8. Run `make gate` when done. All new tests must pass.

---

### /spec-check

Use before completing a phase or raising a PR. Full compliance review.

Run in order — stop and report on first failure:

1. `python scripts/check_boundaries.py` — must print "All boundaries OK"
2. `make lint` — ruff must exit 0
3. `make test` — pytest must exit 0, zero failures
4. **Schema check:** grep for any model in `types.py` missing `frozen=True`
   ```bash
   grep -n "class.*BaseModel" kdx/collector/types.py
   # Every class must have ConfigDict(frozen=True) in its body
   ```
5. **Exit code check:** grep `cli.py` for any `sys.exit` — there should be none, only `raise SystemExit`
6. **Fixture freshness:** confirm all fixture JSON files load without validation errors:
   ```bash
   python -c "from kdx.collector.mock import list_fixtures, load_fixture; [load_fixture(f) for f in list_fixtures()]; print('OK')"
   ```
7. **No TODOs left:** `grep -rn "TODO\|FIXME\|HACK\|XXX" kdx/` — must be empty

Report: list each check as PASS or FAIL. Do not summarise as "mostly passing".

---

### /add-failure-class \<name\>

Use when adding support for a new Kubernetes failure type (e.g. `OOMThrottled`, `Evicted`).

Follow this order exactly:

1. Add the new class string to the `failure_class` literal in `DiagnosisResult` and the classifier priority list in this file.
2. Add a new fixture `tests/fixtures/<name>.json` matching the `DiagnosisContext` schema with `failure_class` set to the new value.
3. Add the new label to `_classify_failure()` in `collector/k8s.py` at the correct priority position.
4. Update the system prompt in `diagnosis/prompts.py` to include the new failure class in the schema comment.
5. Add a scenario YAML in `scenarios/<name>/deployment.yaml`.
6. Add test cases to `test_collector.py` (fixture loads) and `test_engine.py` (engine handles new class).
7. Run `/spec-check` — everything must pass before committing.

Do not add the failure class in only some of these locations. Partial additions break fixture validation.

---

### /docs \<scope\>

Use when documentation needs updating after a code change.

Allowed changes only:
- Docstrings on **public functions that were changed** in this session — one-line summary only, no parameter lists unless the signature is genuinely confusing.
- This `CLAUDE.md` file — only the section that reflects the code you changed (e.g. if you added a field to `DiagnosisContext`, update the schema section here).
- `scenarios/README.md` — only if a new scenario was added.

Not allowed:
- Docstrings on unchanged functions.
- Inline comments on unchanged code.
- A separate `docs/` directory or any new `.md` file not listed above.
- Type annotations on code you did not touch.

After making doc changes, run `make gate` to confirm nothing broke.

---

### /review \<description of what was built\>

Use at end of a phase for a structured code review against this spec.

Check in order:

1. Does the code match the spec in this file exactly? Flag any deviation, including "improvements".
2. Are import boundaries clean? Run `python scripts/check_boundaries.py`.
3. Are all Pydantic models frozen?
4. Does every new public function have at least one test?
5. Are there any features, flags, or behaviours not in the spec?
6. Are there any `TODO`, `FIXME`, `print()`, or `breakpoint()` left in production code?
7. Is `make gate` green?

Output: a numbered list of findings. Each finding: PASS, WARN, or FAIL with file:line if applicable. Do not suggest improvements beyond spec compliance.

---

## Harness

### .claude/settings.json

Hooks that run automatically on every file write:

- **Ruff auto-fix** — runs `ruff check kdx/ --fix` silently after every edit. Keeps code clean without manual intervention. Errors are shown but do not block.
- **Allowed commands** — `pytest`, `ruff`, `kubectl`, `kdx`, scripts in `scripts/`. Destructive kubectl commands (`delete namespace default`, `delete node`) are blocked.

### Makefile targets

| Target | Command | Use when |
|--------|---------|----------|
| `make test` | `pytest tests/ -v` | Running full test suite |
| `make test-fast` | `pytest tests/ -x -q` | Quick check during dev |
| `make lint` | `ruff check kdx/ && ruff format kdx/ --check` | Before committing |
| `make fix` | `ruff check kdx/ --fix && ruff format kdx/` | Fix all auto-fixable issues |
| `make boundaries` | `python scripts/check_boundaries.py` | Check import violations |
| `make gate` | `make lint && make boundaries && make test` | Before marking phase complete |
| `make gate-phase1` | `kdx --version` | Phase 1 only |
| `make up SCENARIO=x` | `bash scripts/apply_scenario.sh x` | Apply a k8s scenario |
| `make down` | `bash scripts/reset_scenario.sh` | Tear down kdx-test namespace |
| `make coverage` | `pytest tests/ --cov=kdx --cov-report=term-missing` | Coverage report |

### scripts/check_boundaries.py

Automated import boundary enforcement. Greps all source files for violations of the import rules. Run as part of `make gate`. Exits non-zero on any violation, printing file and line.

Violations it checks:
- `collector/k8s.py` importing from `diagnosis/` or `output/`
- `collector/mock.py` importing from `diagnosis/` or `output/`
- `diagnosis/engine.py` importing from `collector/k8s` or `collector/mock`
- `diagnosis/prompts.py` importing from `collector/k8s` or `collector/mock`
- `output/formatter.py` importing from `diagnosis/` or `collector/k8s` or `collector/mock`
- `cli.py` containing any business logic (heuristic: no direct k8s SDK calls)
