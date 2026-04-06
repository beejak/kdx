# kdx — Kubernetes Diagnose

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab.svg)](https://www.python.org/)
[![Kubernetes](https://img.shields.io/badge/kubernetes-compatible-326ce5.svg)](https://kubernetes.io/)

**Stop reading logs. Start reading diagnoses.**

`kdx` connects to your Kubernetes cluster, collects every signal from a failing Deployment — events, pod logs, container statuses, resource limits — and asks Claude to produce a plain-English root cause with a copy-pasteable fix. No dashboards. No log diving. One command.

---

## The problem

A deploy breaks in staging. You get a `CrashLoopBackOff`. You now have to:

- Open four tabs: build logs, deploy logs, pod logs, k8s events
- Figure out if it's your code, the config, the infra, or a transient issue
- Do all of this under pressure with no clear starting point

Existing tools show you *more* of the same data. `kdx` gives you a diagnosis.

---

## Demo

```
$ kdx diagnose api-server -n production

╭─ Context ────────────────────────────────────────────────╮
│ api-server / production                                   │
│ Cluster: docker-desktop  ·  Pre-class: OOMKilled          │
╰──────────────────────────────────────────────────────────╯

╭─ Diagnosis ──────────────────────────────────────────────╮
│ OOMKilled  (high)                                         │
│                                                           │
│ Container 'api' is hitting its 256Mi memory limit during  │
│ startup before the JVM heap is fully initialised.         │
╰──────────────────────────────────────────────────────────╯

  • [event] OOMKilling  —  pod/api-server-7d9f  14:23:01
  • [log]   java.lang.OutOfMemoryError: Java heap space
  • [status] exit_code=137  restart_count=8  last_reason=OOMKilled

╭─ fix_command ────────────────────────────────────────────╮
│ kubectl set resources deployment/api-server               │
│   -c api --limits=memory=512Mi -n production              │
╰──────────────────────────────────────────────────────────╯

╭─ fix_explanation ────────────────────────────────────────╮
│ The current 256Mi limit is below the JVM's minimum heap   │
│ requirement at startup. Raising to 512Mi gives the        │
│ process enough headroom to initialise without OOMKilling. │
╰──────────────────────────────────────────────────────────╯
```

---

## What kdx can diagnose

| Failure class | Typical cause | Example signal |
|--------------|---------------|----------------|
| `CrashLoopBackOff` | App exits on startup — bad config, missing dependency | `exit_code=1`, repeated restarts |
| `OOMKilled` | Container exceeds its memory limit | `exit_code=137`, OOMKilling event |
| `ImagePullBackOff` | Image doesn't exist or registry is unreachable | `ErrImagePull` in events |
| `Pending` / Unschedulable | No node satisfies the pod's constraints | `FailedScheduling` event |

---

## Quick start

**Requirements:** Python 3.12+, `kubectl` configured, an [Anthropic API key](https://console.anthropic.com/)

```bash
# 1. Clone and install
git clone https://github.com/beejak/kdx.git
cd kdx
make venv

# 2. Set your API key
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# 3. Diagnose a failing deployment
.venv/bin/kdx diagnose <deployment-name> -n <namespace>
```

No cluster? Try mock mode first — no API key needed for the collection step:

```bash
.venv/bin/kdx diagnose crash-demo --mock crash_loop
```

---

## Installation

### From source (recommended while in beta)

```bash
git clone https://github.com/beejak/kdx.git
cd kdx
make venv          # creates .venv/, installs all dependencies
make gate-phase1   # verify: kdx --version should print 0.1.0
```

### Requirements

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.12+ | Required — uses modern union type syntax |
| kubectl | any | Must be configured and pointing at a cluster |
| Anthropic API key | — | [Get one here](https://console.anthropic.com/) |

---

## Configuration

Create a `.env` file in the project root (copy from `.env.example`):

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Your Claude API key |
| `KDX_MODEL` | No | `claude-sonnet-4-5` | Override the Claude model used |
| `KDX_MAX_TOKENS` | No | `1024` | Override max response tokens |
| `KDX_LOG_LEVEL` | No | `WARNING` | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `KUBECONFIG` | No | `~/.kube/config` | Path to kubeconfig file |

`kdx` loads `.env` automatically — no need to `source` or `export`.

---

## Usage

### Diagnose a deployment

```bash
.venv/bin/kdx diagnose DEPLOYMENT [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-n, --namespace TEXT` | `default` | Kubernetes namespace |
| `--mock FIXTURE` | — | Use a fixture instead of a live cluster |
| `--dump-context PATH` | — | Write the collected data to a JSON file before calling Claude |
| `--context TEXT` | — | Kubeconfig context name to use |

**Examples:**

```bash
# Diagnose a live deployment
.venv/bin/kdx diagnose api-server -n production

# Use a specific kubeconfig context
.venv/bin/kdx diagnose api-server -n staging --context my-gke-cluster

# Diagnose without a cluster (mock mode)
.venv/bin/kdx diagnose crash-demo --mock crash_loop

# Capture the raw collected data for debugging or fixture creation
.venv/bin/kdx diagnose api-server -n production --dump-context /tmp/context.json
```

### Available mock fixtures

| Fixture name | Simulates |
|-------------|-----------|
| `crash_loop` | CrashLoopBackOff — app exits with code 1 |
| `oom_kill` | OOMKilled — container exceeds memory limit |
| `image_pull_backoff` | ImagePullBackOff — invalid image registry |
| `pending_unschedulable` | Pending — unsatisfiable node selector |

---

## Kubernetes setup

### Docker Desktop (local development)

1. Open Docker Desktop → **Settings** → **Kubernetes** → **Enable Kubernetes** → **Apply & Restart**
2. Wait 2–3 minutes for the cluster to start
3. Verify from your terminal:
   ```bash
   kubectl config use-context docker-desktop
   kubectl get nodes
   # NAME             STATUS   ROLES           AGE
   # docker-desktop   Ready    control-plane   2m
   ```
4. **WSL2 users:** if `kdx` times out connecting to the API server, point it at the Windows-side kubeconfig:
   ```bash
   export KUBECONFIG=/mnt/c/Users/<YourUser>/.kube/config
   ```

### Test with real failure scenarios

`kdx` ships with four deliberately broken Deployments to verify your setup end-to-end:

```bash
# Apply a scenario
make up SCENARIO=crash_loop

# Watch the pod enter the failure state
kubectl get pods -n kdx-test -w
# NAME                          READY   STATUS             RESTARTS
# crash-demo-...                0/1     CrashLoopBackOff   3

# Run the diagnosis
.venv/bin/kdx diagnose crash-demo -n kdx-test

# Tear everything down
make down
```

Available scenarios: `crash_loop`, `oom_kill`, `image_pull_backoff`, `pending_unschedulable`

### Remote / production clusters

`kdx` uses your existing `kubectl` configuration. To target a remote cluster:

```bash
# Point at any context in your kubeconfig
.venv/bin/kdx diagnose my-service -n production --context my-prod-cluster
```

**Required RBAC permissions** — `kdx` is read-only and needs:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kdx-reader
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events", "nodes"]
    verbs: ["get", "list"]
```

Apply with:
```bash
kubectl apply -f https://raw.githubusercontent.com/beejak/kdx/main/deploy/rbac.yaml
```

---

## How it works

```
kdx diagnose
     │
     ├─► collector/k8s.py          Connect to cluster via kubeconfig
     │        │                    Fetch: Deployment → Pods → Events → Logs
     │        │                    Pre-classify failure (deterministic rules)
     │        ▼
     │   DiagnosisContext          Immutable snapshot of all signals
     │        │
     ├─► diagnosis/engine.py       Build structured prompt
     │        │                    Call Claude API (claude-sonnet-4-5)
     │        │                    Parse + validate JSON response
     │        ▼
     │   DiagnosisResult           failure_class, root_cause, evidence[], fix_command
     │        │
     └─► output/formatter.py       Render Rich panels to terminal
```

**Data collected per diagnosis:**

- Deployment spec (replicas, image, selector, conditions)
- Up to 5 pods (statuses, restart counts, exit codes)
- Pod events from the last 30 minutes (max 50 per pod)
- Last 100 lines of logs from the failing container
- Last 50 lines of logs from the previous container instance
- Namespace events (last 30 minutes, max 50)

All data stays local except for the structured context sent to the Claude API.

---

## Development

See [CLAUDE.md](CLAUDE.md) for the full architecture, data models, import boundaries, and agent protocols used during development.

```bash
# Run all tests (no cluster needed)
make test

# Run the full gate: lint + boundary check + tests
make gate

# Fix all auto-fixable lint issues
make fix

# Run with coverage
make coverage
```

Tests run entirely in mock mode — no live cluster or API key needed. All 27 tests pass in under 1 second.

---

## Roadmap

- [ ] Slack integration — post diagnosis to a channel when a deploy fails
- [ ] Helm values suggestion — output a ready-to-apply Helm override for the fix
- [ ] Multi-deployment — diagnose all failing deployments in a namespace at once
- [ ] Prometheus integration — include CPU/memory usage metrics in the context
- [ ] `kubectl` plugin — run as `kubectl diagnose`
- [ ] CI/CD integration — GitHub Actions step that diagnoses on deploy failure

---

## Contributing

Contributions are welcome. Before opening a PR:

1. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and workflow
2. Run `make gate` — it must pass
3. Add tests for any new behaviour

---

## License

[MIT](LICENSE) — © 2026 beejak
