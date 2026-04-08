PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF   := .venv/bin/ruff
KDX    := .venv/bin/kdx

.PHONY: test test-fast lint fix boundaries gate gate-phase1 up down coverage venv push-github

# ── Venv setup ────────────────────────────────────────────────────────────────

venv:
	python3 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]" -q

# ── Test ──────────────────────────────────────────────────────────────────────

test:
	$(PYTEST) tests/ -v

test-fast:
	$(PYTEST) tests/ -x -q

coverage:
	$(PYTEST) tests/ --cov=kdx --cov-report=term-missing

# ── Lint / Format ─────────────────────────────────────────────────────────────

lint:
	$(RUFF) check kdx/
	$(RUFF) format kdx/ --check

fix:
	$(RUFF) check kdx/ --fix
	$(RUFF) format kdx/

# ── Boundary check ────────────────────────────────────────────────────────────

boundaries:
	$(PYTHON) scripts/check_boundaries.py

# ── Gates ─────────────────────────────────────────────────────────────────────

gate: lint boundaries test
	@echo "✓ Gate passed"

gate-phase1:
	$(KDX) --version
	@echo "✓ Phase 1 gate passed"

# ── Publish (commit all changes and push to origin/main) ───────────────────────
# Override the subject line, e.g. make push-github COMMIT_SUBJECT='docs: fix README mock section'

COMMIT_SUBJECT ?= chore: sync kdx repo

push-github:
	git add -A
	@git diff --cached --quiet || git commit -m "$(COMMIT_SUBJECT)"
	git push origin main

# ── Scenarios (requires Docker Desktop k8s) ───────────────────────────────────

up:
	@test -n "$(SCENARIO)" || (echo "Usage: make up SCENARIO=crash_loop" && exit 1)
	bash scripts/apply_scenario.sh $(SCENARIO)

down:
	bash scripts/reset_scenario.sh
