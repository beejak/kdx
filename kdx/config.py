from __future__ import annotations

import os

import click

from kdx.diagnosis.providers import LLMProvider


class Settings:
    def __init__(self):
        self.provider: str = os.getenv("KDX_PROVIDER", "anthropic")
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
            click.echo(
                f"[kdx] {key} is not set. Copy .env.example to .env and fill it in.",
                err=True,
            )
            raise SystemExit(2)
        return v


def build_provider(settings: Settings) -> LLMProvider:
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
    click.echo(
        f"[kdx] Unknown provider '{settings.provider}'. Use 'anthropic' or 'openai-compatible'.",
        err=True,
    )
    raise SystemExit(2)
