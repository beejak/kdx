from pathlib import Path

from kdx.collector.types import DiagnosisContext

FIXTURES_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures"


def load_fixture(name: str) -> DiagnosisContext:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No fixture '{name}'. Available: {list_fixtures()}")
    return DiagnosisContext.model_validate_json(path.read_text())


def list_fixtures() -> list[str]:
    return [p.stem for p in FIXTURES_DIR.glob("*.json")]
