"""
Import boundary enforcement for kdx.

Run as: python scripts/check_boundaries.py
Exits 0 if all boundaries are clean, 1 if any violations are found.

Boundaries (from CLAUDE.md):
  collector/k8s.py    → must NOT import from diagnosis/ or output/
  collector/mock.py   → must NOT import from diagnosis/ or output/
  diagnosis/engine.py → must NOT import from collector/k8s or collector/mock
  diagnosis/prompts.py→ must NOT import from collector/k8s or collector/mock
  output/formatter.py → must NOT import from diagnosis/ or collector/k8s or collector/mock
  cli.py              → must NOT contain direct kubernetes SDK calls
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KDX = ROOT / "kdx"

RULES: list[tuple[str, list[str], str]] = [
    (
        "kdx/collector/k8s.py",
        [r"from kdx\.diagnosis", r"from kdx\.output", r"import kdx\.diagnosis", r"import kdx\.output"],
        "collector/k8s.py must not import from diagnosis/ or output/",
    ),
    (
        "kdx/collector/mock.py",
        [r"from kdx\.diagnosis", r"from kdx\.output", r"import kdx\.diagnosis", r"import kdx\.output"],
        "collector/mock.py must not import from diagnosis/ or output/",
    ),
    (
        "kdx/diagnosis/engine.py",
        [r"from kdx\.collector\.k8s", r"from kdx\.collector\.mock",
         r"import kdx\.collector\.k8s", r"import kdx\.collector\.mock"],
        "diagnosis/engine.py must not import from collector/k8s or collector/mock",
    ),
    (
        "kdx/diagnosis/prompts.py",
        [r"from kdx\.collector\.k8s", r"from kdx\.collector\.mock",
         r"import kdx\.collector\.k8s", r"import kdx\.collector\.mock"],
        "diagnosis/prompts.py must not import from collector/k8s or collector/mock",
    ),
    (
        "kdx/output/formatter.py",
        [r"from kdx\.diagnosis", r"from kdx\.collector\.k8s", r"from kdx\.collector\.mock",
         r"import kdx\.diagnosis", r"import kdx\.collector\.k8s", r"import kdx\.collector\.mock"],
        "output/formatter.py must not import from diagnosis/, collector/k8s, or collector/mock",
    ),
    (
        "kdx/diagnosis/providers.py",
        [r"from kdx\.collector\.k8s", r"from kdx\.collector\.mock",
         r"from kdx\.diagnosis\.engine", r"from kdx\.output",
         r"import kdx\.collector\.k8s", r"import kdx\.collector\.mock"],
        "providers.py must only import from collector/types and standard libs",
    ),
    (
        "kdx/cli.py",
        [r"from kubernetes\b", r"import kubernetes\b"],
        "cli.py must not contain direct kubernetes SDK calls — use collector/k8s.py",
    ),
]


def check() -> int:
    violations: list[str] = []

    for rel_path, patterns, message in RULES:
        path = ROOT / rel_path
        if not path.exists():
            continue  # file not created yet — skip silently
        lines = path.read_text().splitlines()
        for lineno, line in enumerate(lines, start=1):
            for pattern in patterns:
                if re.search(pattern, line):
                    violations.append(f"  {rel_path}:{lineno}  {line.strip()}\n  → {message}")
                    break  # one violation per line is enough

    if violations:
        print(f"BOUNDARY VIOLATIONS ({len(violations)} found):\n")
        for v in violations:
            print(v)
            print()
        return 1

    print("All boundaries OK")
    return 0


if __name__ == "__main__":
    sys.exit(check())
