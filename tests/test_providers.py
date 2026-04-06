from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIStatusError

from kdx.collector.types import DiagnosisError
from kdx.config import Settings, build_provider
from kdx.diagnosis.providers import AnthropicProvider, OpenAICompatibleProvider


def test_anthropic_provider_returns_text(mocker):
    mock_cls = mocker.patch("anthropic.Anthropic")
    inst = mock_cls.return_value
    inst.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text="hello")])
    p = AnthropicProvider(api_key="k", model="m", timeout=30.0)
    assert p.complete("sys", "user", 64) == "hello"
    inst.messages.create.assert_called_once()


def test_anthropic_provider_529_raises_diagnosis_error(mocker):
    mock_cls = mocker.patch("anthropic.Anthropic")
    inst = mock_cls.return_value
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(529, request=req)
    exc = APIStatusError("overloaded", response=resp, body={})
    inst.messages.create.side_effect = exc
    p = AnthropicProvider(api_key="k", model="m", timeout=30.0)
    with pytest.raises(DiagnosisError, match="Service overloaded"):
        p.complete("s", "u", 64)


def test_anthropic_provider_other_error_raises_diagnosis_error(mocker):
    mock_cls = mocker.patch("anthropic.Anthropic")
    inst = mock_cls.return_value
    inst.messages.create.side_effect = RuntimeError("boom")
    p = AnthropicProvider(api_key="k", model="m", timeout=30.0)
    with pytest.raises(DiagnosisError, match="boom"):
        p.complete("s", "u", 64)


def test_openai_compatible_provider_returns_text(mocker):
    mock_cls = mocker.patch("openai.OpenAI")
    inst = mock_cls.return_value
    inst.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="local out"))]
    )
    p = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen",
        timeout=120.0,
    )
    assert p.complete("sys", "user", 64) == "local out"


def test_openai_compatible_provider_error_raises_diagnosis_error(mocker):
    mock_cls = mocker.patch("openai.OpenAI")
    inst = mock_cls.return_value
    inst.chat.completions.create.side_effect = RuntimeError("connection refused")
    p = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen",
        timeout=120.0,
    )
    with pytest.raises(DiagnosisError, match="connection refused"):
        p.complete("s", "u", 64)


def test_build_provider_anthropic(monkeypatch):
    monkeypatch.setenv("KDX_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = Settings()
    p = build_provider(s)
    assert isinstance(p, AnthropicProvider)


def test_build_provider_openai_compatible(monkeypatch):
    monkeypatch.setenv("KDX_PROVIDER", "openai-compatible")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KDX_LOCAL_BASE_URL", "http://localhost:11434/v1")
    s = Settings()
    p = build_provider(s)
    assert isinstance(p, OpenAICompatibleProvider)


def test_build_provider_unknown_exits_2(monkeypatch):
    monkeypatch.setenv("KDX_PROVIDER", "garbage")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = Settings()
    with pytest.raises(SystemExit) as exc_info:
        build_provider(s)
    assert exc_info.value.args[0] == 2


def test_anthropic_not_required_for_local_provider(monkeypatch):
    monkeypatch.setenv("KDX_PROVIDER", "openai-compatible")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings()
    assert s.anthropic_api_key == ""
