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
7. [Environment setup guides](#7-environment-setup-guides)
   - [Windows + Docker Desktop (WSL2)](#71-windows--docker-desktop-wsl2)
   - [macOS + Docker Desktop](#72-macos--docker-desktop)
   - [Linux + minikube or kind](#73-linux--minikube-or-kind)
   - [Remote cluster (GKE / EKS / AKS)](#74-remote-cluster-gke--eks--aks)
   - [Inside a Kubernetes pod (in-cluster)](#75-inside-a-kubernetes-pod-in-cluster)
8. [LLM provider setup](#8-llm-provider-setup)
   - [Anthropic Claude (default)](#81-anthropic-claude-default)
   - [Ollama (local)](#82-ollama-local)
   - [LM Studio (local)](#83-lm-studio-local)
   - [vLLM (self-hosted)](#84-vllm-self-hosted)
9. [Mock mode and fixtures](#9-mock-mode-and-fixtures)
10. [Test scenarios](#10-test-scenarios)
11. [Troubleshooting](#11-troubleshooting)

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

`kdx` automates that entire left side. It connects to your cluster, collects every relevant signal from the failing Deployment, and asks an LLM to produce a single diagnosis:

```
  k8s Events ──┐
  Pod logs ────┤
  Container    ├──► kdx ──► LLM ──► Root cause + Fix command
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
              │  Build LLM provider                │
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
         │  2. Call LLM provider (attempt 1)                │
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

Before calling the LLM, `kdx` deterministically classifies the failure from container statuses. This grounds the prompt and reduces hallucination risk.

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

### LLM provider selection

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
| LLM provider | — | Anthropic API key **or** local model via Ollama/LM Studio |

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
| `KDX_TIMEOUT` | No | `30` (Anthropic) / `120` (local) | HTTP timeout in seconds |
| `KDX_LOCAL_BASE_URL` | No | `http://localhost:11434/v1` | Base URL for OpenAI-compatible provider |
| `KDX_LOCAL_API_KEY` | No | `ollama` | API key for local provider (any string for Ollama) |
| `KUBECONFIG` | No | `~/.kube/config` | Path to kubeconfig file |
| `KDX_LOG_LEVEL` | No | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Minimal .env for Anthropic

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
| `--dump-context PATH` | | — | Write collected data to JSON before calling LLM |
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

## 7. Environment setup guides

### 7.1 Windows + Docker Desktop (WSL2)

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
# Edit .env and add ANTHROPIC_API_KEY=sk-ant-...
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

### 7.2 macOS + Docker Desktop

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
# Edit .env — add ANTHROPIC_API_KEY
```

**Step 4 — Verify**

```bash
kubectl config use-context docker-desktop
kubectl get nodes
make gate-phase1
```

---

### 7.3 Linux + minikube or kind

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

### 7.4 Remote cluster (GKE / EKS / AKS)

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

### 7.5 Inside a Kubernetes pod (in-cluster)

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
              key: anthropic-api-key
```

---

## 8. LLM provider setup

### 8.1 Anthropic Claude (default)

**Best for:** Production use, highest diagnosis quality.

```
Your machine ──► api.anthropic.com ──► Claude Sonnet
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

### 8.2 Ollama (local)

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

### 8.3 LM Studio (local)

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

### 8.4 vLLM (self-hosted)

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

## 9. Mock mode and fixtures

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

This captures the full `DiagnosisContext` before the LLM call. You can then replay it offline:

```bash
.venv/bin/kdx diagnose api-server --mock my_real_failure
```

### Fixture format

Fixtures are plain JSON files in `tests/fixtures/`. They must match the `DiagnosisContext` schema defined in `kdx/collector/types.py`. Run `make test` after adding a new fixture — the `test_all_fixtures_are_valid` test validates all fixtures automatically.

---

## 10. Test scenarios

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

## 11. Troubleshooting

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

**Cause:** The LLM returned prose instead of a JSON object. This happens mainly with small local models.

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

### `DiagnosisError: Claude is overloaded, try again`

**Cause:** Anthropic API returned HTTP 529 (service overloaded).

**Fix:** Wait 30–60 seconds and try again. If persistent, check [status.anthropic.com](https://status.anthropic.com).

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
