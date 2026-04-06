import json
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIStatusError

from kdx.collector.mock import load_fixture
from kdx.collector.types import DiagnosisError, DiagnosisResult
from kdx.config import Settings
from kdx.diagnosis import engine


def _fake_result_dict(failure_class: str = "CrashLoopBackOff") -> dict:
    return {
        "failure_class": failure_class,
        "root_cause": "Container exits on startup.",
        "evidence": ["[pod] crash-demo restart loop"],
        "fix_command": "kubectl logs deploy/crash-demo -n kdx-test",
        "fix_explanation": "Inspect logs to confirm.",
        "confidence": "high",
    }


def _patch_create(mocker, text: str):
    mock_client = mocker.patch("kdx.diagnosis.engine.Anthropic").return_value
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=text)],
    )


@pytest.mark.parametrize(
    "fixture",
    ["crash_loop", "oom_kill", "image_pull_backoff", "pending_unschedulable"],
)
def test_diagnose_all_fixtures(mocker, fixture):
    ctx = load_fixture(fixture)
    payload = json.dumps(_fake_result_dict())
    _patch_create(mocker, payload)
    settings = Settings()
    out = engine.diagnose(ctx, settings)
    assert isinstance(out, DiagnosisResult)
    assert out.failure_class == "CrashLoopBackOff"


def test_diagnose_fenced_json(mocker):
    ctx = load_fixture("crash_loop")
    inner = json.dumps(_fake_result_dict())
    _patch_create(mocker, f"Here is JSON:\n```json\n{inner}\n```")
    settings = Settings()
    out = engine.diagnose(ctx, settings)
    assert out.root_cause == "Container exits on startup."


def test_diagnose_bad_json_raises(mocker):
    ctx = load_fixture("crash_loop")
    _patch_create(mocker, "not valid json at all {{{")
    settings = Settings()
    with pytest.raises(DiagnosisError, match="Invalid diagnosis response"):
        engine.diagnose(ctx, settings)


def test_diagnose_api_529_raises(mocker):
    ctx = load_fixture("crash_loop")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(529, request=req)
    exc = APIStatusError("overloaded", response=resp, body={})
    mock_client = mocker.patch("kdx.diagnosis.engine.Anthropic").return_value
    mock_client.messages.create.side_effect = exc
    settings = Settings()
    with pytest.raises(DiagnosisError, match="Claude is overloaded"):
        engine.diagnose(ctx, settings)
