# kdx Help Guide

> **One command. Every signal. Plain-English diagnosis.**

---

## Table of Contents

1. [What kdx does](#1-what-kdx-does)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Configuration](#5-configuration)
6. [Usage reference](#6-usage-reference)
7. [How kdx connects to your cluster](#7-how-kdx-connects-to-your-cluster)
8. [Working with diagnosis results](#8-working-with-diagnosis-results)
9. [Environment setup guides](#9-environment-setup-guides)
   - [Windows + Docker Desktop (WSL2)](#91-windows--docker-desktop-wsl2)
   - [macOS + Docker Desktop](#92-macos--docker-desktop)
   - [Linux + minikube or kind](#93-linux--minikube-or-kind)
   - [Remote cluster (GKE / EKS / AKS)](#94-remote-cluster-gke--eks--aks)
   - [Inside a Kubernetes pod (in-cluster)](#95-inside-a-kubernetes-pod-in-cluster)
10. [Model provider setup](#10-model-provider-setup)
    - [Anthropic (default)](#101-anthropic-default)
    - [Ollama (local)](#102-ollama-local)
    - [LM Studio (local)](#103-lm-studio-local)
    - [vLLM (self-hosted)](#104-vllm-self-hosted)
11. [Mock mode and fixtures](#11-mock-mode-and-fixtures)
12. [Test scenarios](#12-test-scenarios)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What kdx does

When a Kubernetes Deployment breaks, you normally have to manually cross-reference four sources of information under pressure:

```
  Build logs    Deploy logs    Pod logs    k8s Events
      │               │            │            │
      └───────────────┴────────────┴────────────┘
                            │
                   You figure it out
```

`kdx` automates that entire left side. It connects to your cluster, collects every relevant signal from the failing Deployment, and produces a single diagnosis:

```
  k8s Events ──┐
  Pod logs ────┤
  Container    ├──► kdx ──► diagnosis engine ──► Root cause + Fix command
  statuses ────┤
  Resource     │
  limits ──────┘
```

**Example output:**

```
╭─ Diagnosis ─────────────────────────────────────────────╮
│ OOMKilled  (high confidence)                             │
│                                                          │
│ Container 'api' hit its 256Mi memory limit at startup.   │
╰──────────────────────────────────────────────────────────╯

  • [event] OOMKilling — pod/api-server-7d9f  14:23:01
  • [log]   java.lang.OutOfMemoryError: Java heap space
  • [status] exit_code=137  restart_count=8

╭─ fix_command ────────────────────────────────────────────╮
│ kubectl set resources deployment/api-server              │
│   -c api --limits=memory=512Mi -n production             │
╰──────────────────────────────────────────────────────────╯
```

---

## 2. How it works

### Full data flow

```
┌─────────────────────────────────────────────────────────────────┐
│  kdx diagnose api-server -n production                          │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │      cli.py           │
                    │  Parse args + flags   │
                    └───────────┬───────────┘
                                │
              ┌─────────────────▼──────────────────┐
              │         config.py                  │
              │  Load .env + env vars              │
              │  Build model provider               │
              │  (Anthropic or OpenAI-compatible)  │
              └─────────────────┬──────────────────┘
                                │
         ┌──────────────────────▼──────────────────────────┐
         │              collector/k8s.py                   │
         │                                                  │
         │  1. Load kubeconfig / in-cluster config          │
         │  2. Fetch Deployment                             │
         │  3. Find pods via label selector                 │
         │  4. Fetch pod events (last 30 min, max 50)       │
         │  5. Fetch namespace events (last 30 min, max 50) │
         │  6. Read logs (tail 100) + prev logs (tail 50)   │
         │  7. Pre-classify failure class                   │
         │                                                  │
         │  Returns: DiagnosisContext (frozen, immutable)   │
         └──────────────────────┬──────────────────────────┘
                                │
         ┌──────────────────────▼──────────────────────────┐
         │             diagnosis/engine.py                 │
         │                                                  │
         │  1. Build prompt from DiagnosisContext           │
         │  2. Call model provider (attempt 1)              │
         │  3. Parse JSON response                          │
         │  4. If parse fails → retry with stripped prompt  │
         │  5. Validate against DiagnosisResult schema      │
         │                                                  │
         │  Returns: DiagnosisResult                        │
         └──────────────────────┬──────────────────────────┘
                                │
         ┌──────────────────────▼──────────────────────────┐
         │             output/formatter.py                 │
         │  Render Rich panels to terminal                  │
         └─────────────────────────────────────────────────┘
```

### Pre-classification logic

Before calling the model provider, `kdx` deterministically classifies the failure from container statuses. This grounds the prompt and improves result quality.

```
Container statuses
        │
        ├── Any container: reason=OOMKilled or last_state_reason=OOMKilled?
        │       └── YES → failure_class = "OOMKilled"
        │
        ├── Any container: reason=CrashLoopBackOff?
        │       └── YES → failure_class = "CrashLoopBackOff"
        │
        ├── Any container: reason=ImagePullBackOff or ErrImagePull?
        │       └── YES → failure_class = "ImagePullBackOff"
        │
        ├── All pods: phase=Pending?
        │       └── YES → failure_class = "Pending"
        │
        └── None of the above → failure_class = "Unknown"
```

### Model provider selection

```
KDX_PROVIDER env var
        │
        ├── "anthropic" (default)
        │       └── AnthropicProvider
        │               └── anthropic SDK → api.anthropic.com
        │
        └── "openai-compatible"
                └── OpenAICompatibleProvider
                        └── openai SDK → KDX_LOCAL_BASE_URL
                                ├── Ollama  → http://localhost:11434/v1
                                ├── LM Studio → http://localhost:1234/v1
                                └── vLLM → http://your-server/v1
```

---

## 3. Requirements

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Python | 3.12 | Uses modern union type syntax (`str \| None`) |
| kubectl | any | Must be configured and pointing at a cluster |
| Kubernetes cluster | v1.20+ | Local or remote |
| Model provider | — | Anthropic API key **or** local model via Ollama/LM Studio |

---

## 4. Installation

### From source

```bash
git clone https://github.com/beejak/kdx.git
cd kdx
make venv
```

`make venv` creates `.venv/` using Python 3.12 and installs all dependencies. Always use `make <target>` — never bare `pytest` or `python` — to ensure the correct Python is used.

### Verify

```bash
make gate-phase1
# Output: kdx, version 0.1.0
#         ✓ Phase 1 gate passed
```

---

## 5. Configuration

### .env file

Copy the example and fill in your values:

```bash
cp .env.example .env
```

kdx loads `.env` automatically on startup — no `source` or `export` needed.

### Full environment variable reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | When using Anthropic | — | Get one at [console.anthropic.com](https://console.anthropic.com) |
| `KDX_PROVIDER` | No | `anthropic` | `anthropic` or `openai-compatible` |
| `KDX_MODEL` | No | Provider default | `claude-sonnet-4-5` (Anthropic) or `llama3.1:8b` (local) |
| `KDX_MAX_TOKENS` | No | `1024` | Maximum response tokens |
| `KDX_TIMEOUT` | No | `30` (Anthropic) / `120` (local) | HTTP timeout in seconds (increase for slow local models) |
| `KDX_LOCAL_BASE_URL` | No | `http://localhost:11434/v1` | Base URL for OpenAI-compatible provider |
| `KDX_LOCAL_API_KEY` | No | `ollama` | API key for local provider (any string for Ollama) |
| `KUBECONFIG` | No | `~/.kube/config` | Path to kubeconfig file |
| `KDX_LOG_LEVEL` | No | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Minimal .env for hosted provider (Anthropic)

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

### Minimal .env for Ollama (local)

```bash
KDX_PROVIDER=openai-compatible
KDX_MODEL=qwen2.5:7b
```

---

## 6. Usage reference

### Command syntax

```
.venv/bin/kdx diagnose DEPLOYMENT [OPTIONS]
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--namespace TEXT` | `-n` | `default` | Kubernetes namespace |
| `--mock FIXTURE` | | — | Use a fixture file instead of a live cluster |
| `--dump-context PATH` | | — | Write collected data to JSON before running diagnosis |
| `--context TEXT` | | current context | Kubeconfig context to use |

### Common usage patterns

```bash
# Diagnose a deployment in the default namespace
.venv/bin/kdx diagnose api-server

# Specify namespace
.venv/bin/kdx diagnose api-server -n production

# Use a specific kubeconfig context
.venv/bin/kdx diagnose api-server -n staging --context my-gke-cluster

# Diagnose without a cluster (mock mode — no API key needed for collection)
.venv/bin/kdx diagnose crash-demo --mock crash_loop

# Save the collected data for later use or debugging
.venv/bin/kdx diagnose api-server -n production --dump-context /tmp/context.json

# Re-diagnose saved data (no cluster needed)
.venv/bin/kdx diagnose api-server --mock /tmp/context.json
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Diagnosis completed successfully |
| `1` | Diagnosis error — cluster unreachable, API failure, or parse error |
| `2` | Configuration error — missing env var or invalid fixture name |

---

## 7. How kdx connects to your cluster

### Connection mechanism

kdx uses the official Kubernetes Python client, which follows the same kubeconfig resolution as `kubectl`. No separate credentials or setup are needed beyond what `kubectl` already has.

```
kdx diagnose api-server -n production
         │
         ▼
1. Check --context flag → use that context if set
         │
         ▼
2. Load kubeconfig
   ├── $KUBECONFIG env var (if set)
   ├── ~/.kube/config (default)
   └── /mnt/c/Users/<user>/.kube/config (WSL2 — set KUBECONFIG if needed)
         │
         ▼
3. If no kubeconfig found → try in-cluster config
   (automatic when kdx runs inside a pod with a mounted ServiceAccount)
         │
         ▼
4. Connect to Kubernetes API server (HTTPS, 10s timeout)
```

The `--context` flag maps directly to a named context in your kubeconfig. To list available contexts:

```bash
kubectl config get-contexts
```

### What kdx reads (and what it doesn't)

kdx is **strictly read-only**. It only calls `get` and `list` — it never creates, modifies, or deletes anything in your cluster.

| API call | What it fetches | Why |
|----------|----------------|-----|
| `AppsV1Api.read_namespaced_deployment` | Deployment spec, replicas, conditions, image | Understand intended state |
| `AppsV1Api.list_namespaced_replica_set` | Latest 2 ReplicaSets | Find pods owned by the deployment |
| `CoreV1Api.list_namespaced_pod` | Up to 5 pods matching the deployment's label selector | Get pod statuses and restart history |
| `CoreV1Api.list_namespaced_event` (pod-scoped) | Pod events from the last 30 minutes | Spot OOMKilling, BackOff, scheduling failures |
| `CoreV1Api.list_namespaced_event` (namespace) | Namespace events from the last 30 minutes, max 50 | Catch node-level or scheduler events |
| `CoreV1Api.read_namespaced_pod_log` | Last 100 lines of the failing container's logs | See what the app printed before crashing |
| `CoreV1Api.read_namespaced_pod_log` (previous) | Last 50 lines from the prior container instance | See the crash output, not the retry startup |
| `CoreV1Api.list_node` | Node labels and capacity | Diagnose unschedulable pods |

### Minimum RBAC permissions

If you are on a shared or production cluster, your user or service account needs these permissions. kdx will surface a clear error if any are missing.

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

Apply the pre-built role from the repo:

```bash
kubectl apply -f https://raw.githubusercontent.com/beejak/kdx/main/deploy/rbac.yaml
```

Then bind it to your user or service account:

```bash
# Bind to a user
kubectl create clusterrolebinding kdx-reader-binding \
  --clusterrole=kdx-reader \
  --user=<your-user>

# Or bind to a service account (for in-cluster use)
kubectl create clusterrolebinding kdx-reader-binding \
  --clusterrole=kdx-reader \
  --serviceaccount=<namespace>:<serviceaccount-name>
```

To check what your current user can do:

```bash
kubectl auth can-i list pods -n production
kubectl auth can-i get deployments -n production
kubectl auth can-i list events -n production
kubectl auth can-i get nodes
```

### Namespace scoping

All collection is scoped to the namespace you pass with `-n`. kdx never reads across namespaces. The only exception is node information, which is cluster-scoped — nodes are read when diagnosing `Pending` pods to check scheduling constraints.

### Timeouts

| Operation | Timeout | Override |
|-----------|---------|---------|
| Kubernetes API calls | 10 seconds | Not configurable — fast enough for any reachable cluster |
| Model provider API | 30s (hosted) / 120s (local) | `KDX_TIMEOUT` env var |

If the Kubernetes API times out, `kdx` exits with code `1` and prints the connection error. Check `kubectl cluster-info` to verify the API server is reachable.

---

## 8. Working with diagnosis results

### Reading the output

A successful diagnosis produces four panels:

```
╭─ Context ─────────────────────────────────────────────────╮
│ api-server / production                                    │
│ Cluster: my-gke-cluster  ·  Pre-class: OOMKilled           │
╰────────────────────────────────────────────────────────────╯
```

**Context panel** — confirms which deployment and namespace was diagnosed, which cluster kdx connected to, and the pre-classified failure class (determined from raw container statuses before the model call).

```
╭─ Diagnosis ───────────────────────────────────────────────╮
│ OOMKilled  (high)                                          │
│                                                            │
│ Container 'api' hit its 256Mi memory limit at startup      │
│ before the JVM heap was fully initialised.                 │
╰────────────────────────────────────────────────────────────╯

  • [event] OOMKilling — pod/api-server-7d9f  14:23:01
  • [log]   java.lang.OutOfMemoryError: Java heap space
  • [status] exit_code=137  restart_count=8  last_reason=OOMKilled
```

**Diagnosis panel** — the failure class, confidence level, and a 1–2 sentence root cause. Below it, the evidence list shows the exact signals that support the diagnosis, each tagged with its source (`[event]`, `[log]`, `[status]`, `[pod]`).

```
╭─ fix_command ─────────────────────────────────────────────╮
│ kubectl set resources deployment/api-server                │
│   -c api --limits=memory=512Mi -n production               │
╰────────────────────────────────────────────────────────────╯

╭─ fix_explanation ─────────────────────────────────────────╮
│ The current 256Mi limit is below the JVM's minimum heap    │
│ requirement. Raising to 512Mi gives the process enough     │
│ headroom to initialise without being killed.               │
╰────────────────────────────────────────────────────────────╯
```

**fix_command** — a complete, copy-pasteable `kubectl` command or YAML patch. It is always scoped to the correct deployment and namespace from your original command.

**fix_explanation** — why the fix works, in plain English.

### Confidence levels

| Level | Meaning | Recommended action |
|-------|---------|-------------------|
| `high` | The failure class is unambiguous from the evidence. Root cause and fix are reliable. | Apply the fix directly. |
| `medium` | The evidence points in one direction but some signals are missing or ambiguous. | Review the evidence list before applying. Cross-check with your application's known behaviour. |
| `low` | The failure class is unclear or the signals are contradictory. | Treat as a starting point, not a definitive answer. Use `--dump-context` and investigate manually. |

### Applying the fix

The `fix_command` is ready to run. Copy it from the terminal and execute it:

```bash
# Example: raise memory limit
kubectl set resources deployment/api-server \
  -c api --limits=memory=512Mi -n production

# After applying, watch the rollout
kubectl rollout status deployment/api-server -n production

# Confirm pods are healthy
kubectl get pods -n production -l app=api-server
```

For fixes that produce a YAML patch instead of a `kubectl` command, pipe it directly:

```bash
# If fix_command outputs a YAML patch, apply it with:
kubectl apply -f - <<'EOF'
<paste the YAML from fix_command here>
EOF
```

### After the fix — verifying recovery

```bash
# 1. Watch the rollout
kubectl rollout status deployment/<name> -n <namespace>

# 2. Check new pods are running
kubectl get pods -n <namespace>

# 3. Confirm no more restarts after a few minutes
kubectl get pods -n <namespace> -w

# 4. Check events are clean
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -20
```

If the pods return to the same failure state, run `kdx diagnose` again — the new signals after the attempted fix may reveal a deeper layer.

### When confidence is low or the fix doesn't work

1. **Capture the full context** for offline investigation:
   ```bash
   .venv/bin/kdx diagnose <deployment> -n <namespace> \
     --dump-context /tmp/context.json
   ```
   Open `context.json` — it contains every signal kdx collected, in full. Look for signals that don't appear in the evidence list.

2. **Check the logs yourself** — kdx truncates to 100 lines. If the failure is buried deeper:
   ```bash
   kubectl logs deployment/<name> -n <namespace> --tail=500
   kubectl logs deployment/<name> -n <namespace> --previous --tail=200
   ```

3. **Check events across the namespace**:
   ```bash
   kubectl get events -n <namespace> --sort-by='.lastTimestamp'
   ```

4. **Re-run with more context** — if you changed config or environment after the initial diagnosis, run `kdx diagnose` again. kdx always collects fresh signals; it does not cache.

### Using --dump-context

The `--dump-context` flag writes the collected `DiagnosisContext` JSON to a file before the model call. This is useful when:

- You want to inspect exactly what was sent to the model provider
- You want to replay a diagnosis without hitting the cluster again
- You are filing a bug report and need to share the raw signals
- You want to build a new fixture for offline testing

```bash
# Capture
.venv/bin/kdx diagnose api-server -n production \
  --dump-context /tmp/api-context.json

# Replay offline (uses the saved context, skips cluster collection)
.venv/bin/kdx diagnose api-server --mock /tmp/api-context.json
```

The JSON file is plain text and contains no credentials — only Kubernetes resource data that was already visible via `kubectl`.

---

## 9. Environment setup guides

### 9.1 Windows + Docker Desktop (WSL2)

**This is the most common development setup.**

```
┌─────────────────────────────────────────────────────────┐
│  Windows host                                            │
│                                                          │
│  ┌──────────────────────┐   ┌────────────────────────┐  │
│  │   Docker Desktop     │   │   WSL2 (Ubuntu)        │  │
│  │                      │   │                        │  │
│  │   Kubernetes API ────┼───┼──► kubectl             │  │
│  │   127.0.0.1:6443     │   │   kdx                  │  │
│  │                      │   │   python3              │  │
│  │   kubeconfig at:     │   │                        │  │
│  │   C:\Users\<user>\   │   │   /root/cicd/          │  │
│  │   .kube\config       │   │   (this repo)          │  │
│  └──────────────────────┘   └────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Step 1 — Enable Kubernetes**

Docker Desktop → Settings → Kubernetes → Enable Kubernetes → Apply & Restart

Wait 2–3 minutes. The status indicator turns green.

**Step 2 — Verify from WSL2**

```bash
kubectl config get-contexts
# CURRENT   NAME             CLUSTER          AUTHINFO
# *         docker-desktop   docker-desktop   docker-desktop

kubectl get nodes
# NAME             STATUS   ROLES           AGE
# docker-desktop   Ready    control-plane   3m
```

**Step 3 — Install kdx**

```bash
cd /path/to/kdx   # your WSL2 path, e.g. /root/cicd or ~/projects/kdx
make venv
cp .env.example .env
# Edit .env and add your provider API key
```

**Step 4 — Run a diagnosis**

```bash
.venv/bin/kdx diagnose crash-demo --mock crash_loop   # offline test
```

**WSL2-specific troubleshooting**

If `kdx` times out connecting to the Kubernetes API:

```bash
# Option 1 — use the Windows-side kubeconfig directly
export KUBECONFIG=/mnt/c/Users/<YourUser>/.kube/config

# Option 2 — add to your .env
echo "KUBECONFIG=/mnt/c/Users/<YourUser>/.kube/config" >> .env
```

Docker Desktop routes `127.0.0.1:6443` from WSL2 through a virtual network adapter. If this breaks after a Windows update or Docker Desktop upgrade, restart Docker Desktop and try again.

---

### 9.2 macOS + Docker Desktop

```
┌──────────────────────────────────────────────────────────┐
│  macOS host                                               │
│                                                          │
│  Docker Desktop          Terminal                        │
│  ┌─────────────────┐     ┌──────────────────────────┐   │
│  │ Kubernetes API  │     │  kubectl                 │   │
│  │ 127.0.0.1:6443  ◄─────┤  kdx                     │   │
│  │                 │     │  python3.12               │   │
│  │ kubeconfig at:  │     │                           │   │
│  │ ~/.kube/config  │     │  ~/projects/kdx/          │   │
│  └─────────────────┘     └──────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

**Step 1 — Enable Kubernetes**

Docker Desktop → Settings → Kubernetes → Enable Kubernetes → Apply & Restart

**Step 2 — Install Python 3.12**

```bash
brew install python@3.12
python3.12 --version   # 3.12.x
```

**Step 3 — Install kdx**

```bash
git clone https://github.com/beejak/kdx.git
cd kdx
make venv
cp .env.example .env
# Edit .env — add your provider API key
```

**Step 4 — Verify**

```bash
kubectl config use-context docker-desktop
kubectl get nodes
make gate-phase1
```

---

### 9.3 Linux + minikube or kind

**Using minikube**

```bash
# Install minikube
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
install minikube-linux-amd64 /usr/local/bin/minikube

# Start a local cluster
minikube start

# Verify
kubectl get nodes
# NAME       STATUS   ROLES           AGE
# minikube   Ready    control-plane   1m

# Install kdx
git clone https://github.com/beejak/kdx.git
cd kdx
make venv
cp .env.example .env
```

**Using kind**

```bash
# Install kind
go install sigs.k8s.io/kind@latest   # or download binary

# Create a cluster
kind create cluster --name kdx-test

# Verify
kubectl cluster-info --context kind-kdx-test

# Install kdx
git clone https://github.com/beejak/kdx.git
cd kdx
make venv
```

---

### 9.4 Remote cluster (GKE / EKS / AKS)

kdx uses your existing kubeconfig. Any cluster you can reach with `kubectl` works with `kdx`.

```
Your machine                         Remote cluster
┌──────────────────┐                 ┌──────────────────────────┐
│                  │   HTTPS         │                          │
│  kdx             ├────────────────►│  Kubernetes API server   │
│  kubectl         │  (kubeconfig)   │                          │
│                  │                 │  - Deployments           │
└──────────────────┘                 │  - Pods                  │
                                     │  - Events                │
                                     │  - Logs                  │
                                     └──────────────────────────┘
```

**GKE**

```bash
# Authenticate and get credentials
gcloud container clusters get-credentials my-cluster \
  --zone us-central1-a --project my-project

# Verify
kubectl config get-contexts   # find the GKE context name

# Diagnose
.venv/bin/kdx diagnose api-server -n production \
  --context gke_my-project_us-central1-a_my-cluster
```

**EKS**

```bash
# Update kubeconfig
aws eks update-kubeconfig --name my-cluster --region us-east-1

# Diagnose
.venv/bin/kdx diagnose api-server -n production \
  --context arn:aws:eks:us-east-1:123456789:cluster/my-cluster
```

**AKS**

```bash
# Get credentials
az aks get-credentials --resource-group my-rg --name my-cluster

# Diagnose
.venv/bin/kdx diagnose api-server -n production --context my-cluster
```

**RBAC — required permissions**

For real clusters, apply the read-only role before running `kdx`:

```bash
kubectl apply -f https://raw.githubusercontent.com/beejak/kdx/main/deploy/rbac.yaml
```

This creates a `ClusterRole` with read access to Deployments, ReplicaSets, Pods, pod logs, Events, and Nodes. Edit `deploy/rbac.yaml` to bind it to your service account or user.

---

### 9.5 Inside a Kubernetes pod (in-cluster)

`kdx` detects when it is running inside a pod and loads the in-cluster config automatically (no kubeconfig needed):

```
┌───────────────────────────────────────────────────────────┐
│  Kubernetes cluster                                        │
│                                                            │
│  ┌─────────────────────┐      ┌────────────────────────┐  │
│  │  kdx pod            │      │  Kubernetes API        │  │
│  │                     │      │                        │  │
│  │  ServiceAccount ────┼─────►│  /apis/apps/v1/...     │  │
│  │  (with kdx-reader   │      │  /api/v1/pods/...      │  │
│  │   ClusterRole)      │      │  /api/v1/events/...    │  │
│  └─────────────────────┘      └────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

kdx tries `load_kube_config()` first. If it fails (no kubeconfig), it falls back to `load_incluster_config()` automatically.

**Example pod manifest**

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: kdx-runner
  namespace: kdx-system
spec:
  serviceAccountName: kdx-sa   # must have kdx-reader ClusterRole
  containers:
    - name: kdx
      image: python:3.12-slim
      command: ["sh", "-c", "pip install kdx && kdx diagnose api-server -n production"]
      env:
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: kdx-secrets
              key: provider-api-key
```

---

## 10. Model provider setup

### 10.1 Anthropic (default)

**Best for:** Production use, highest diagnosis quality.

```
Your machine ──► api.anthropic.com ──► Anthropic model
```

**Setup:**

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. Add to `.env`:
   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   ```

**Timeout:** 30 seconds (hardcoded default, override with `KDX_TIMEOUT`)

**Cost:** Approximately $0.003–$0.015 per diagnosis depending on context size.

---

### 10.2 Ollama (local)

**Best for:** Development, air-gapped environments, zero API cost.

```
Your machine
┌───────────────────────────────────────┐
│                                        │
│  kdx ──► OpenAICompatibleProvider      │
│               │                        │
│               ▼                        │
│          Ollama server                 │
│          localhost:11434               │
│               │                        │
│               ▼                        │
│          Local model                   │
│          (qwen2.5:7b, llama3.1:8b)     │
└───────────────────────────────────────┘
```

**Step 1 — Install Ollama**

```bash
# Linux / WSL2
curl -fsSL https://ollama.com/install.sh | sh

# macOS
brew install ollama
```

**Step 2 — Pull a model**

| Model | Command | RAM needed | JSON quality |
|-------|---------|------------|-------------|
| `qwen2.5:7b` ⭐ | `ollama pull qwen2.5:7b` | ~5GB | Excellent |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | ~6GB | Good |
| `mistral:7b` | `ollama pull mistral:7b` | ~5GB | Good |
| `llama3.2:3b` | `ollama pull llama3.2:3b` | ~2GB | Fair |

`qwen2.5:7b` is the recommended model — it produces the most reliable JSON output.

**Step 3 — Start Ollama**

```bash
ollama serve   # starts on http://localhost:11434
```

**Step 4 — Configure kdx**

```bash
# .env
KDX_PROVIDER=openai-compatible
KDX_MODEL=qwen2.5:7b
# KDX_LOCAL_BASE_URL=http://localhost:11434/v1  (this is the default)
# KDX_LOCAL_API_KEY=ollama                      (this is the default)
```

**Step 5 — Test**

```bash
.venv/bin/kdx diagnose crash-demo --mock crash_loop
```

**Note on JSON reliability:** Local models occasionally wrap their response in prose on the first attempt. `kdx` automatically retries with a stripped-down prompt. If you see `WARN: retrying with simplified prompt`, this is expected behaviour, not an error.

---

### 10.3 LM Studio (local)

**Best for:** Windows users who prefer a GUI for model management.

```
Windows / macOS
┌───────────────────────────────────────────┐
│                                            │
│  kdx (WSL2 or native)                      │
│       │                                    │
│       ▼                                    │
│  OpenAICompatibleProvider                  │
│       │                                    │
│       ▼                                    │
│  LM Studio local server                   │
│  localhost:1234 (default)                  │
│       │                                    │
│       ▼                                    │
│  Any GGUF model loaded in LM Studio        │
└───────────────────────────────────────────┘
```

**Step 1** — Download [LM Studio](https://lmstudio.ai) and load a model (e.g. Qwen2.5-7B-Instruct-GGUF)

**Step 2** — Start the local server: LM Studio → Local Server tab → Start Server

**Step 3 — Configure kdx**

```bash
# .env
KDX_PROVIDER=openai-compatible
KDX_MODEL=<model-name-as-shown-in-lmstudio>
KDX_LOCAL_BASE_URL=http://localhost:1234/v1
KDX_LOCAL_API_KEY=lmstudio
```

**WSL2 note:** LM Studio runs on Windows. From WSL2, replace `localhost` with the Windows host IP:

```bash
# Get Windows host IP from WSL2
cat /etc/resolv.conf | grep nameserver | awk '{print $2}'

# Use in .env
KDX_LOCAL_BASE_URL=http://172.x.x.x:1234/v1
```

---

### 10.4 vLLM (self-hosted)

**Best for:** Teams running a shared GPU inference server.

```
Your machine ──────────────────► vLLM server
                                 (GPU instance)
                                 http://your-server:8000/v1
```

**Configure kdx:**

```bash
# .env
KDX_PROVIDER=openai-compatible
KDX_MODEL=mistralai/Mistral-7B-Instruct-v0.3
KDX_LOCAL_BASE_URL=http://your-vllm-server:8000/v1
KDX_LOCAL_API_KEY=your-vllm-api-key
KDX_TIMEOUT=60
```

---

## 11. Mock mode and fixtures

Mock mode lets you run `kdx` without a live cluster or an API key (for the collection step only). It loads a pre-captured `DiagnosisContext` JSON from `tests/fixtures/`.

```bash
# Available built-in fixtures
.venv/bin/kdx diagnose crash-demo   --mock crash_loop
.venv/bin/kdx diagnose oom-demo     --mock oom_kill
.venv/bin/kdx diagnose badimage     --mock image_pull_backoff
.venv/bin/kdx diagnose pending-demo --mock pending_unschedulable
```

### Capturing a fixture from a live cluster

```bash
.venv/bin/kdx diagnose api-server -n production \
  --dump-context tests/fixtures/my_real_failure.json
```

This captures the full `DiagnosisContext` before the diagnosis call. You can then replay it offline:

```bash
.venv/bin/kdx diagnose api-server --mock my_real_failure
```

### Fixture format

Fixtures are plain JSON files in `tests/fixtures/`. They must match the `DiagnosisContext` schema defined in `kdx/collector/types.py`. Run `make test` after adding a new fixture — the `test_all_fixtures_are_valid` test validates all fixtures automatically.

---

## 12. Test scenarios

kdx ships with four Kubernetes manifests that create deliberately broken Deployments, so you can test the full live diagnosis flow.

### Applying and tearing down scenarios

```bash
# Apply a scenario
make up SCENARIO=crash_loop

# Watch the pod reach its failure state
kubectl get pods -n kdx-test -w

# Run the diagnosis
.venv/bin/kdx diagnose <deployment-name> -n kdx-test

# Clean up everything
make down
```

### Scenario reference

| Scenario | Deployment name | Failure state | Wait time |
|----------|----------------|---------------|-----------|
| `crash_loop` | `crash-demo` | `CrashLoopBackOff` | ~30s |
| `oom_kill` | `oom-demo` | `OOMKilled` → `CrashLoopBackOff` | ~15s |
| `image_pull_backoff` | `badimage-demo` | `ImagePullBackOff` | ~10s |
| `pending_unschedulable` | `pending-demo` | `Pending` | immediate |

### What each scenario does

**crash_loop** — runs `busybox` with a command that prints a fake database error and exits with code 1. After 3–5 restarts, Kubernetes enters `CrashLoopBackOff` with exponential backoff.

```
Pod starts → prints "ERROR: cannot connect to db:5432" → exits(1)
         ↑                                                    │
         └──────────── Kubernetes restarts (backoff) ─────────┘
```

**oom_kill** — runs a Python one-liner that allocates 400MB of memory under a 128Mi container limit. The kernel OOM killer terminates the process with `SIGKILL` (exit code 137).

```
Pod starts → Python allocates 400MB → OOM killer → exit(137)
                                         │
                                    exit_code=137
                                    reason=OOMKilled
```

**image_pull_backoff** — references a registry that does not exist (`registry.does-not-exist.invalid`). The kubelet tries to pull the image, fails, and enters `ImagePullBackOff`.

```
kubelet → DNS lookup for registry.does-not-exist.invalid → NXDOMAIN
       → reason=ErrImagePull → reason=ImagePullBackOff
```

**pending_unschedulable** — requests a node with `disktype=ssd` label. No node in the local cluster has this label, so the pod is never scheduled.

```
kube-scheduler → find node with disktype=ssd label → none found
              → FailedScheduling event
              → pod stays in Pending indefinitely
```

---

## 13. Troubleshooting

### `[kdx] ANTHROPIC_API_KEY is not set`

**Cause:** Running `kdx diagnose` with `KDX_PROVIDER=anthropic` but no API key.

**Fix:**
```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

---

### `kdx needs read access to Deployments, Pods, Events in namespace X. Check RBAC.`

**Cause:** The kubeconfig user/service account does not have the required read permissions.

**Fix:**
```bash
kubectl apply -f deploy/rbac.yaml
# Edit deploy/rbac.yaml first to set the correct namespace and service account name
```

To check what permissions your current user has:
```bash
kubectl auth can-i list pods -n production
kubectl auth can-i get deployments -n production
kubectl auth can-i list events -n production
```

---

### `Connection timeout` when connecting to the cluster

**WSL2 — Docker Desktop:**
```bash
export KUBECONFIG=/mnt/c/Users/<YourUser>/.kube/config
```

**Remote cluster:**
- Check VPN connectivity
- Verify the cluster API server endpoint: `kubectl cluster-info`
- Increase timeout: `KDX_TIMEOUT=60`

---

### `DiagnosisError: No JSON found in response`

**Cause:** The model returned prose instead of a JSON object. This happens mainly with small local models.

**Fix — use a larger model:**
```bash
# .env
KDX_MODEL=qwen2.5:7b   # minimum recommended for reliable JSON output
```

**Fix — increase timeout** (model may have timed out mid-response):
```bash
KDX_TIMEOUT=180
```

`kdx` automatically retries once with a stripped-down prompt. If it still fails, the model is likely too small or too slow.

---

### `DiagnosisError: Service overloaded (HTTP 529), try again`

**Cause:** The provider API returned HTTP 529 (service overloaded).

**Fix:** Wait 30–60 seconds and try again. If persistent for Anthropic, check [status.anthropic.com](https://status.anthropic.com).

---

### `ModuleNotFoundError` or wrong Python version

**Cause:** Running `pytest` or `python` directly instead of through the Makefile.

**Fix:** Always use `make <target>` which pins to `.venv/bin/`:
```bash
make test        # not: pytest tests/
make gate        # not: ruff check kdx/ && pytest
```

---

### Fixture fails to load: `No fixture 'x'. Available: [...]`

**Cause:** The fixture name doesn't match a file in `tests/fixtures/`.

**Fix:** List available fixtures:
```bash
python3 -c "from kdx.collector.mock import list_fixtures; print(list_fixtures())"
```

---

### `TypeError: unsupported operand type(s) for |`

**Cause:** Running with Python < 3.10. The `str | None` union syntax requires Python 3.10+; this project requires 3.12.

**Fix:**
```bash
python3 --version   # must be 3.12.x
make venv           # rebuilds .venv with the correct Python
```

---

*For issues not covered here, open a GitHub issue at [beejak/kdx](https://github.com/beejak/kdx/issues).*
