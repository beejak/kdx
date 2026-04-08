# kdx — Kubernetes Diagnose

## What it does

`kdx diagnose <deployment> -n <namespace>` connects to the current kubeconfig context, collects
Kubernetes signals for a failing Deployment, sends them to the configured LLM provider (Anthropic
or OpenAI-compatible, e.g. Ollama), and prints a structured root-cause diagnosis with a
copy-pasteable fix.

Target failure classes: CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending/Unschedulable.

---

## Tech stack

- Python 3.12, Click 8, Rich 13, Pydantic v2, anthropic SDK, openai SDK, python-dotenv, kubernetes Python client
- pip + pyproject.toml (no Poetry)
- pytest + pytest-mock for tests
- ruff for lint/format

---

## Project layout

```
kdx/
├── __init__.py               # __version__ = "0.1.0"
├── cli.py                    # Click entry point — thin orchestration only
├── config.py                 # Settings dataclass, env var loading, build_provider() factory
├── collector/
│   ├── types.py              # ALL shared Pydantic models (DiagnosisContext, DiagnosisResult, etc.)
│   ├── k8s.py                # Live collector — kubernetes SDK only, no subprocess
│   └── mock.py               # Loads DiagnosisContext from tests/fixtures/<name>.json
├── diagnosis/
│   ├── prompts.py            # System prompt, RETRY_SYSTEM_PROMPT, build_context_message()
│   ├── providers.py          # LLMProvider protocol, AnthropicProvider, OpenAICompatibleProvider
│   └── engine.py             # Calls provider.complete(), parses result, handles retry
└── output/
    └── formatter.py          # Rich terminal output

tests/
├── conftest.py               # ANTHROPIC_API_KEY=test-key dummy, shared fixtures
├── fixtures/                 # JSON files: crash_loop.json, oom_kill.json,
│   │                         #   image_pull_backoff.json, pending_unschedulable.json
├── test_collector.py
├── test_prompts.py
├── test_engine.py
├── test_providers.py
├── test_formatter.py
└── test_cli.py

scenarios/
├── crash_loop/deployment.yaml
├── oom_kill/deployment.yaml
├── image_pull_backoff/deployment.yaml
└── pending_unschedulable/deployment.yaml

scripts/
├── apply_scenario.sh         # kubectl apply -f scenarios/$1/ -n kdx-test (creates ns if needed)
└── reset_scenario.sh         # kubectl delete namespace kdx-test --ignore-not-found
```

Repo root (user-facing docs — keep aligned with `config.py`, `cli.py`, and this file): `README.md`, `docs/help.md`, `examples/llm_input_format.md`, `examples/diagnosis_context.sample.json` (canonical sample of raw `DiagnosisContext` JSON for humans and tools).

---

## Import boundaries — enforced, never violated

```
cli.py → config.py, collector/*, diagnosis/*, output/*
collector/k8s.py → collector/types.py only
collector/mock.py → collector/types.py only
diagnosis/engine.py → collector/types.py, diagnosis/prompts.py, diagnosis/providers.py
diagnosis/prompts.py → collector/types.py only
diagnosis/providers.py → collector/types.py only
output/formatter.py → collector/types.py only
```

`diagnosis/engine.py` receives `DiagnosisContext` and an `LLMProvider` as arguments.
It never imports from `collector/k8s.py` or `collector/mock.py`.
All shared types live in `collector/types.py` — the only cross-boundary import allowed.

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

Run this before the LLM call. Priority order when multiple signals conflict:

1. **OOMKilled** — any ContainerStatus where `reason == "OOMKilled"` or `last_state_reason == "OOMKilled"`
2. **CrashLoopBackOff** — any ContainerStatus where `reason == "CrashLoopBackOff"`
3. **ImagePullBackOff** — any ContainerStatus where `reason in ("ErrImagePull", "ImagePullBackOff")`
4. **Pending** — all pods have `phase == "Pending"` and none match rules 1–3
5. **Unknown** — fallback

Return the string label. This grounds the model prompt in a known class and reduces hallucination.

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
def diagnose(ctx: DiagnosisContext, provider: LLMProvider, max_tokens: int = 1024) -> DiagnosisResult:
    ...
```

Engine receives a `LLMProvider` instance (not `Settings`). It never constructs a provider itself.

### Call + retry loop

```python
def diagnose(ctx: DiagnosisContext, provider: LLMProvider, max_tokens: int = 1024) -> DiagnosisResult:
    user_content = build_context_message(ctx)
    for attempt in range(2):
        prompt = SYSTEM_PROMPT if attempt == 0 else RETRY_SYSTEM_PROMPT
        raw = provider.complete(prompt, user_content, max_tokens)
        try:
            parsed = _extract_json(raw)
            return DiagnosisResult.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, DiagnosisError, ValueError) as exc:
            if attempt == 1:
                raise DiagnosisError(f"Invalid diagnosis response: {raw[:500]}") from exc
    raise DiagnosisError("Diagnosis failed")  # unreachable — satisfies type checker
```

**Why 2 attempts:** Local models (< 7B parameters) often wrap their response in prose or markdown on the first try. The retry uses `RETRY_SYSTEM_PROMPT` — a stripped-down prompt that repeats the JSON-only instruction. Claude (Anthropic) rarely needs the retry but the logic applies uniformly to all providers.

### Response parsing

```python
def _extract_json(text: str) -> dict:
    dec = json.JSONDecoder()
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return dec.raw_decode(text[m.start(1):])[0]
    # Fall back to raw JSON
    brace = text.find("{")
    if brace == -1:
        raise DiagnosisError(f"No JSON found in response: {text[:200]}")
    return dec.raw_decode(text[brace:])[0]
```

On `json.JSONDecodeError` or `ValidationError` → the loop retries once, then raises `DiagnosisError`.

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

### RETRY_SYSTEM_PROMPT

Used on the second attempt when the first response fails JSON parsing. Shorter and more forceful — aimed at local models that wrap their output in prose.

```
Output ONLY a valid JSON object. No markdown. No explanation. No prose.
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
        self.provider: str = os.getenv("KDX_PROVIDER", "anthropic")
        # API key only required for anthropic provider
        if self.provider == "anthropic":
            self.anthropic_api_key: str = self._require("ANTHROPIC_API_KEY")
        else:
            self.anthropic_api_key: str = ""
        default_timeout = "30" if self.provider == "anthropic" else "120"
        self.timeout: float = float(os.getenv("KDX_TIMEOUT", default_timeout))
        self.model: str = os.getenv(
            "KDX_MODEL",
            "claude-sonnet-4-5" if self.provider == "anthropic" else "llama3.1:8b",
        )
        self.max_tokens: int = int(os.getenv("KDX_MAX_TOKENS", "1024"))
        self.local_base_url: str = os.getenv("KDX_LOCAL_BASE_URL", "http://localhost:11434/v1")
        self.local_api_key: str = os.getenv("KDX_LOCAL_API_KEY", "ollama")

    @staticmethod
    def _require(key: str) -> str:
        v = os.getenv(key)
        if not v:
            import click
            click.echo(f"[kdx] {key} is not set. Copy .env.example to .env and fill it in.", err=True)
            raise SystemExit(2)
        return v
```

`build_provider(settings: Settings) -> LLMProvider` is a factory in `config.py`:

```python
def build_provider(settings: Settings) -> "LLMProvider":
    from kdx.diagnosis.providers import AnthropicProvider, OpenAICompatibleProvider
    if settings.provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.model,
            timeout=settings.timeout,
        )
    if settings.provider == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=settings.local_base_url,
            api_key=settings.local_api_key,
            model=settings.model,
            timeout=settings.timeout,
        )
    import click
    click.echo(f"[kdx] Unknown provider '{settings.provider}'. Use 'anthropic' or 'openai-compatible'.", err=True)
    raise SystemExit(2)
```

`cli.py` calls `load_dotenv()` at import (from `python-dotenv`) so a `.env` in the process working directory is merged into the environment before `Settings()` reads vars. Shell-exported variables still apply; by default dotenv does not override existing env keys.

After `DiagnosisContext` is built (`collect()` or `load_fixture()`), and after optional `--dump-context`, `cli.py` constructs `Settings()`, then `build_provider(settings)`, then `run_diagnosis(ctx, provider, settings.max_tokens)`. `ANTHROPIC_API_KEY` is only required when `KDX_PROVIDER=anthropic`.

---

## CLI (cli.py)

Entry: `load_dotenv()` then Click group with `kdx --help` / `kdx --version`.

```
kdx diagnose DEPLOYMENT [OPTIONS]

Options (see `kdx diagnose --help` for full text):
  -n, --namespace TEXT     Kubernetes namespace [default: default]
  --mock FIXTURE           Built-in fixture stem (tests/fixtures/<FIXTURE>.json), not a path
  --dump-context PATH      Write DiagnosisContext JSON to PATH before calling the model
  --context NAME           Kubeconfig context name (default: current context)
```

`DEPLOYMENT` is required by the CLI even when `--mock` is set; in mock mode it is not used for collection.

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

This applies to every test automatically. Tests that need `Settings()` with `KDX_PROVIDER=anthropic` rely on this key; tests for `openai-compatible` may `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` explicitly.

### test_engine.py

Mock an `LLMProvider` (e.g. `mocker.MagicMock()` with `complete` returning JSON text). Never patch the Anthropic or OpenAI SDK in engine tests. Never make real API calls.
Test all four fixtures, retry-on-bad-JSON, and failure after two bad responses. Provider-level 529 behaviour is tested in `test_providers.py`.

### test_providers.py

Patch `anthropic.Anthropic` / `openai.OpenAI` at the call site in `providers.py`. Test `build_provider()` for both providers and unknown `KDX_PROVIDER`.

### test_cli.py

Use `click.testing.CliRunner`; patch `kdx.diagnosis.engine.diagnose` where a full run is not needed.

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
build-backend = "setuptools.build_meta"

[project]
name = "kdx"
version = "0.1.0"
description = "AI-powered Kubernetes deployment failure diagnosis"
requires-python = ">=3.12"
dependencies = [
    "click>=8.1",
    "python-dotenv>=1.0",
    "rich>=13.7",
    "pydantic>=2.5",
    "anthropic>=0.25",
    "openai>=1.0",
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

[tool.ruff.format]
quote-style = "double"

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

### Git and GitHub — default after substantive edits

**Remote:** `origin` → `https://github.com/beejak/kdx.git`, branch **`main`**.

When you (the agent) change **code, tests, or user-facing docs** (`README.md`, `docs/help.md`, `examples/*.md`, `CLAUDE.md`, `pyproject.toml`, etc.) and the user’s goal is to ship that work, treat **commit + push** as part of the same task — **do not wait** for a separate “now push” message unless the user explicitly wants local-only work.

1. Run `make gate` (or at least `make test`) before committing when Python changed.
2. From the repo root in **WSL** (`cd /root/cicd`), publish:
   ```bash
   make push-github COMMIT_SUBJECT="feat: short imperative summary"
   ```
   Override `COMMIT_SUBJECT` with a **specific** Conventional-Commits–style line (e.g. `docs: align README with CLI`, `fix: handle 529 from Anthropic`). If you omit it, the Makefile default applies (fine for bulk doc sync only).
3. If `git push` fails (auth, network, sandbox): say so plainly and paste the error; do not claim GitHub is updated.

**Why this is not always automatic from the IDE agent**

- **Credentials:** `git push` over HTTPS needs a cached credential, PAT, or SSH agent — headless/agent sandboxes often cannot supply these.
- **Safety:** Pushing without an explicit check can publish WIP, secrets, or broken commits; the human still owns `origin`.
- **Environment:** Some agent terminals do not attach to WSL or swallow `git` output, so the model must **verify** with `git status` / remote or ask the user to run `make push-github` once if verification fails.

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
8. `kdx/diagnosis/prompts.py` — SYSTEM_PROMPT, RETRY_SYSTEM_PROMPT, build_context_message()
9. `kdx/diagnosis/providers.py` — LLMProvider protocol, AnthropicProvider, OpenAICompatibleProvider
10. Update `kdx/config.py` — add provider fields to Settings, add build_provider()
11. `kdx/diagnosis/engine.py` — calls provider.complete(), retry loop, _extract_json()
12. `tests/test_providers.py` — all provider tests
13. Update `tests/test_engine.py` — mock provider directly (not Anthropic SDK)
14. `kdx/output/formatter.py` — Rich panels
15. Wire `kdx/cli.py` fully — `load_dotenv()` at import, all options + Click `help=` text, exit codes, `build_provider()` after context
16. `tests/test_formatter.py` + `tests/test_cli.py` — smoke tests
17. `kdx/collector/k8s.py` — live collector (requires Docker Desktop k8s)
18. `scenarios/` YAMLs + `scripts/` — manual QA end-to-end

---

## Cursor discipline — hard rules

These apply to every task. No exceptions, no "just this once".

1. **Do not add anything not in this spec.** No extra methods, no convenience helpers, no additional CLI flags, no config options, no logging calls beyond what's described. If you think something is missing, stop and ask.
2. **Do not add comments or docstrings to code you did not change.** Only add a comment when the logic is genuinely non-obvious. Never add docstrings as a pass.
3. **Do not add error handling for scenarios that cannot happen.** Trust Pydantic validation, trust frozen models, trust the import boundaries. Only validate at system entry points (CLI args, API responses, fixture files).
4. **Import boundaries are a hard stop.** If implementing a feature requires violating a boundary in the import rules section, stop and ask — do not work around it.
5. **Run `make gate` before marking any phase complete.** If the gate fails, fix it before moving on. Do not move to the next phase with a failing gate.
6. **Tests use mock mode only.** No test ever hits a live cluster or makes a real model API call. Engine tests mock `LLMProvider`; provider tests patch the SDK inside `providers.py`. Load fixtures via `load_fixture()`.
7. **Schema is frozen.** Do not add, rename, or remove fields from `DiagnosisContext` or `DiagnosisResult` without updating fixtures, prompts, and tests in the same commit.
8. **One concern per file.** `cli.py` orchestrates. `k8s.py` collects. `engine.py` calls the provider. `formatter.py` renders. If you find yourself putting business logic in `cli.py` or k8s calls in `engine.py`, stop.
9. **Ship completed work to GitHub by default.** If the task was to implement or fix something in-repo and nothing said “local only”, finish with `make push-github COMMIT_SUBJECT="…"` (after `make gate` when code changed). If push cannot be verified from the environment, state that and give the exact command the user should run.

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

**No prefix — routine implementation or docs:** When the work is meant to land on GitHub, end with commit + push per **Dev workflow → Git and GitHub** and **Cursor discipline** rule 9 (`make push-github COMMIT_SUBJECT="…"` from `/root/cicd` after `make gate` if code changed).

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
5. **Engine tests:** mock the provider directly — `mocker.MagicMock(spec=LLMProvider)`. Never patch the Anthropic or OpenAI SDK in engine tests. Never construct a real `Settings()` — the `autouse` fixture in `conftest.py` covers the env var.
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
- `README.md`, `docs/help.md`, `examples/*.md` — only to match behaviour already implemented (env vars, CLI flags, flows).
- `scenarios/README.md` — only if a new scenario was added.

Not allowed:
- Docstrings on unchanged functions.
- Inline comments on unchanged code.
- New top-level doc directories beyond `docs/` and `examples/` as already used.
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
| `make test` | `$(PYTEST) tests/ -v` | Running full test suite |
| `make test-fast` | `$(PYTEST) tests/ -x -q` | Quick check during dev |
| `make lint` | `$(RUFF) check kdx/` + `$(RUFF) format kdx/ --check` | Before committing |
| `make fix` | `$(RUFF) check kdx/ --fix` + `$(RUFF) format kdx/` | Fix auto-fixable issues |
| `make boundaries` | `$(PYTHON) scripts/check_boundaries.py` | Check import violations |
| `make gate` | `lint` + `boundaries` + `test` | Before marking phase complete |
| `make gate-phase1` | `$(KDX) --version` | Phase 1 only |
| `make up SCENARIO=x` | `bash scripts/apply_scenario.sh x` | Apply a k8s scenario |
| `make down` | `bash scripts/reset_scenario.sh` | Tear down kdx-test namespace |
| `make coverage` | `$(PYTEST) tests/ --cov=kdx --cov-report=term-missing` | Coverage report |

`Makefile` defines `PYTHON`, `PYTEST`, `RUFF`, and `KDX` as `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/kdx`.

### scripts/check_boundaries.py

Automated import boundary enforcement. Greps all source files for violations of the import rules. Run as part of `make gate`. Exits non-zero on any violation, printing file and line.

Violations it checks:
- `collector/k8s.py` importing from `diagnosis/` or `output/`
- `collector/mock.py` importing from `diagnosis/` or `output/`
- `diagnosis/engine.py` importing from `collector/k8s` or `collector/mock`
- `diagnosis/prompts.py` importing from `collector/k8s` or `collector/mock`
- `diagnosis/providers.py` importing from `collector/k8s`, `collector/mock`, `diagnosis/engine`, or `output/`
- `output/formatter.py` importing from `diagnosis/` or `collector/k8s` or `collector/mock`
- `cli.py` containing any business logic (heuristic: no direct k8s SDK calls)

---

## Local LLM support (diagnosis/providers.py)

### Overview

`kdx` supports two LLM providers: `anthropic` (default) and `openai-compatible` (Ollama, LM Studio, vLLM, or any server that speaks the OpenAI chat completions API). The provider is selected via `KDX_PROVIDER`. All diagnosis logic is identical — only the transport layer changes.

**Critical disclaimer:** Local models (especially < 7B parameters) do not reliably emit clean JSON on the first attempt. They frequently wrap responses in prose, markdown, or add preambles. The engine runs a 2-attempt retry loop — on parse failure, it retries with `RETRY_SYSTEM_PROMPT`, which is shorter and more forceful. This retry is provider-agnostic (applies to all providers) but is most commonly triggered by local models.

Minimum recommended model size: **7B parameters**. Models below 3B will not reliably follow the JSON schema even with the retry prompt.

### LLMProvider protocol (providers.py)

```python
from typing import Protocol, runtime_checkable
from kdx.collector.types import DiagnosisError

@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, user: str, max_tokens: int) -> str:
        """Call the LLM. Returns raw text. Raises DiagnosisError on any failure."""
        ...
```

`complete()` is the only method. It handles all provider-specific error translation — callers only ever see `DiagnosisError` or a string back.

### AnthropicProvider

```python
class AnthropicProvider:
    def __init__(self, api_key: str, model: str, timeout: float):
        from anthropic import Anthropic
        self._client = Anthropic(api_key=api_key, timeout=timeout)
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        from anthropic import APIStatusError
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status is None and getattr(e, "response", None) is not None:
                status = getattr(e.response, "status_code", None)
            if status == 529:
                raise DiagnosisError("Claude is overloaded, try again") from e
            raise DiagnosisError(str(e)) from e
        except Exception as e:
            raise DiagnosisError(str(e)) from e
        return msg.content[0].text
```

### OpenAICompatibleProvider

```python
class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float):
        from openai import OpenAI
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as e:
            raise DiagnosisError(str(e)) from e
        return resp.choices[0].message.content or ""
```

### Updated engine.py signature

```python
def diagnose(ctx: DiagnosisContext, provider: LLMProvider, max_tokens: int = 1024) -> DiagnosisResult:
```

`engine.py` imports `LLMProvider` from `diagnosis/providers.py`. It never imports `Settings`, `AnthropicProvider`, or `OpenAICompatibleProvider` — it works with the protocol only.

### Updated cli.py

At module import: `load_dotenv()` (optional `.env` in cwd). Inside `diagnose` after context is ready:

```python
settings = Settings()
provider = build_provider(settings)
result = run_diagnosis(ctx, provider, settings.max_tokens)
```

### Environment variables (full table including local LLM)

There is **no** `KDX_LOG_LEVEL` in code today — do not document it until `config.py` implements logging.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Only if `KDX_PROVIDER=anthropic` | — | Anthropic API key |
| `KDX_PROVIDER` | No | `anthropic` | `anthropic` or `openai-compatible` |
| `KDX_MODEL` | No | See below | Model name. Default: `claude-sonnet-4-5` (anthropic) or `llama3.1:8b` (openai-compatible) |
| `KDX_MAX_TOKENS` | No | `1024` | Max response tokens |
| `KDX_TIMEOUT` | No | See below | HTTP timeout in seconds. Default: `30` (anthropic) or `120` (openai-compatible) |
| `KDX_LOCAL_BASE_URL` | No | `http://localhost:11434/v1` | Base URL for openai-compatible provider |
| `KDX_LOCAL_API_KEY` | No | `ollama` | API key for local provider (Ollama accepts any string) |
| `KUBECONFIG` | No | platform default | Same as `kubectl` — path to kubeconfig file(s) |

### Recommended local models

| Model | Pull command | JSON reliability | Notes |
|-------|-------------|-----------------|-------|
| `qwen2.5:7b` | `ollama pull qwen2.5:7b` | Excellent | Best local option for structured output |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | Good | General-purpose, widely tested |
| `mistral:7b` | `ollama pull mistral:7b` | Good | Fast on CPU |
| `llama3.2:3b` | `ollama pull llama3.2:3b` | Fair | Only for RAM-constrained machines |

Models below 3B are not supported — they cannot reliably maintain the JSON schema.

### Quick start with Ollama

```bash
# Install Ollama (https://ollama.com)
ollama pull qwen2.5:7b

# Configure kdx to use local model
echo "KDX_PROVIDER=openai-compatible" >> .env
echo "KDX_MODEL=qwen2.5:7b" >> .env

# Run — no Anthropic key needed
.venv/bin/kdx diagnose crash-demo --mock crash_loop
```

### Tests (tests/test_providers.py)

```python
# Tests to write — follow existing patterns from test_engine.py

def test_anthropic_provider_returns_text(mocker): ...
    # patch anthropic.Anthropic, verify complete() returns msg.content[0].text

def test_anthropic_provider_529_raises_diagnosis_error(mocker): ...
    # APIStatusError(status_code=529) → DiagnosisError("Claude is overloaded")

def test_anthropic_provider_other_error_raises_diagnosis_error(mocker): ...
    # Any other exception → DiagnosisError

def test_openai_compatible_provider_returns_text(mocker): ...
    # patch openai.OpenAI, verify complete() returns choices[0].message.content

def test_openai_compatible_provider_error_raises_diagnosis_error(mocker): ...
    # Any exception → DiagnosisError

def test_build_provider_anthropic(monkeypatch): ...
    # KDX_PROVIDER=anthropic, ANTHROPIC_API_KEY set → returns AnthropicProvider

def test_build_provider_openai_compatible(monkeypatch): ...
    # KDX_PROVIDER=openai-compatible → returns OpenAICompatibleProvider, no API key needed

def test_build_provider_unknown_exits_2(monkeypatch): ...
    # KDX_PROVIDER=garbage → SystemExit(2)

def test_anthropic_not_required_for_local_provider(monkeypatch): ...
    # KDX_PROVIDER=openai-compatible, ANTHROPIC_API_KEY unset → Settings() does not raise
```

### Updated test_engine.py pattern

Engine tests mock the provider directly, not the underlying SDK:

```python
def _mock_provider(mocker, text: str):
    provider = mocker.MagicMock()
    provider.complete.return_value = text
    return provider

def test_diagnose_all_fixtures(mocker):
    for fixture in ("crash_loop", "oom_kill", "image_pull_backoff", "pending_unschedulable"):
        ctx = load_fixture(fixture)
        provider = _mock_provider(mocker, json.dumps(_fake_result_dict()))
        result = engine.diagnose(ctx, provider)
        assert isinstance(result, DiagnosisResult)

def test_diagnose_retries_on_bad_json(mocker):
    # First call returns garbage, second call returns valid JSON
    provider = mocker.MagicMock()
    provider.complete.side_effect = ["not json <<<", json.dumps(_fake_result_dict())]
    ctx = load_fixture("crash_loop")
    result = engine.diagnose(ctx, provider)
    assert provider.complete.call_count == 2
    # Second call must use RETRY_SYSTEM_PROMPT
    assert provider.complete.call_args_list[1][0][0] == RETRY_SYSTEM_PROMPT

def test_diagnose_raises_after_two_failures(mocker):
    provider = mocker.MagicMock()
    provider.complete.return_value = "still not json"
    ctx = load_fixture("crash_loop")
    with pytest.raises(DiagnosisError):
        engine.diagnose(ctx, provider)
    assert provider.complete.call_count == 2
```

### Boundary checker (scripts/check_boundaries.py)

The `providers.py` rule is already part of `check_boundaries.py` (see **Harness** above).

### Phase gate for provider feature

After implementing providers:

```bash
make gate   # must pass with 0 failures
# Verify local provider works (requires Ollama running):
KDX_PROVIDER=openai-compatible KDX_MODEL=qwen2.5:7b \
  .venv/bin/kdx diagnose crash-demo --mock crash_loop
```
