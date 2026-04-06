import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)


class Settings:
    def __init__(self):
        self.anthropic_api_key: str = self._require("ANTHROPIC_API_KEY")
        self.model: str = os.getenv("KDX_MODEL", "claude-sonnet-4-5")
        self.max_tokens: int = int(os.getenv("KDX_MAX_TOKENS", "1024"))

    @staticmethod
    def _require(key: str) -> str:
        v = os.getenv(key)
        if not v:
            import click

            click.echo(
                f"[kdx] {key} is not set. Copy .env.example to .env and fill it in.", err=True
            )
            raise SystemExit(2)
        return v
