# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] — 2026-04-06

### Added

- `kdx diagnose DEPLOYMENT` command — connects to the current kubeconfig context and returns an AI-generated root-cause diagnosis
- Four failure classes: `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `Pending`
- Deterministic pre-classifier grounds the Claude prompt before the API call
- `--mock FIXTURE` flag — run diagnoses without a live cluster using JSON fixtures
- `--dump-context PATH` flag — capture the raw collected data for debugging or fixture creation
- `--context TEXT` flag — target a specific kubeconfig context
- Four test scenarios: `crash_loop`, `oom_kill`, `image_pull_backoff`, `pending_unschedulable`
- `make gate` — single command that runs lint, import boundary check, and full test suite
- `.env` auto-loading via `python-dotenv`
- 10-second timeout on Kubernetes API calls
- 30-second timeout on Claude API calls with `529` (overloaded) error handling
