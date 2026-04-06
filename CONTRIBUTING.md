# Contributing to kdx

Thanks for your interest. This document covers how to get set up, how the codebase is structured, and what the bar is for a PR to be accepted.

## Setup

```bash
git clone https://github.com/beejak/kdx.git
cd kdx
make venv          # creates .venv/ with Python 3.12, installs all deps
make gate          # must pass before you write a single line
```

No cluster required for development. All tests run in mock mode.

## Architecture

The full architecture — data models, import boundaries, file ownership, and design decisions — is in [CLAUDE.md](CLAUDE.md). Read it before making changes.

The short version:

```
cli.py → collector/ → diagnosis/ → output/
```

These layers have hard import boundaries. `diagnosis/engine.py` never imports from `collector/k8s.py`. `output/formatter.py` never imports from `diagnosis/`. Violations are caught by `make boundaries` and will block a PR.

## Development workflow

```bash
make test          # run all tests
make test-fast     # stop on first failure, minimal output
make lint          # ruff check + format check
make fix           # auto-fix all ruff issues
make boundaries    # check import boundary violations
make gate          # lint + boundaries + tests — run this before every commit
```

**Always use `make <target>`, never bare `pytest` or `ruff`.** The Makefile pins all commands to `.venv/bin/` so you always run the correct Python.

## Agent protocols (for AI-assisted development)

If you use an AI coding assistant, CLAUDE.md defines specific protocols for common tasks:

- `/debug <error>` — structured 5-step triage
- `/gen-tests <module>` — test generation following project conventions
- `/spec-check` — full compliance review before a PR
- `/add-failure-class <name>` — step-by-step guide for extending to a new k8s failure type
- `/docs` — constrained documentation updates
- `/review` — structured code review against the spec

## Adding a new failure class

1. Add the class string to the classifier priority list in `collector/k8s.py`
2. Add a fixture to `tests/fixtures/<name>.json`
3. Add a scenario YAML to `scenarios/<name>/deployment.yaml`
4. Update the system prompt in `diagnosis/prompts.py`
5. Add tests in `test_collector.py` and `test_engine.py`
6. Run `make gate`

See the `/add-failure-class` protocol in CLAUDE.md for the full checklist.

## Pull request checklist

- [ ] `make gate` passes
- [ ] New behaviour has tests
- [ ] No features added beyond what the PR describes
- [ ] Import boundaries not violated (`make boundaries` is clean)
- [ ] No `TODO`, `FIXME`, `print()`, or `breakpoint()` in production code

## What we won't merge

- Features not discussed in an issue first
- PRs that skip `make gate`
- Changes to `DiagnosisContext` or `DiagnosisResult` without updating fixtures, prompts, and tests
- Docstrings or comments added to code that wasn't changed in the PR
