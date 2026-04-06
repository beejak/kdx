import json

import pytest

from kdx.collector.mock import load_fixture
from kdx.collector.types import DiagnosisError, DiagnosisResult
from kdx.diagnosis import engine
from kdx.diagnosis.prompts import RETRY_SYSTEM_PROMPT


def _fake_result_dict(failure_class: str = "CrashLoopBackOff") -> dict:
    return {
        "failure_class": failure_class,
        "root_cause": "Container exits on startup.",
        "evidence": ["[pod] crash-demo restart loop"],
        "fix_command": "kubectl logs deploy/crash-demo -n kdx-test",
        "fix_explanation": "Inspect logs to confirm.",
        "confidence": "high",
    }


def _mock_provider(mocker, text: str):
    provider = mocker.MagicMock()
    provider.complete.return_value = text
    return provider


@pytest.mark.parametrize(
    "fixture",
    ["crash_loop", "oom_kill", "image_pull_backoff", "pending_unschedulable"],
)
def test_diagnose_all_fixtures(mocker, fixture):
    ctx = load_fixture(fixture)
    payload = json.dumps(_fake_result_dict())
    provider = _mock_provider(mocker, payload)
    out = engine.diagnose(ctx, provider)
    assert isinstance(out, DiagnosisResult)
    assert out.failure_class == "CrashLoopBackOff"


def test_diagnose_fenced_json(mocker):
    ctx = load_fixture("crash_loop")
    inner = json.dumps(_fake_result_dict())
    provider = _mock_provider(mocker, f"Here is JSON:\n```json\n{inner}\n```")
    out = engine.diagnose(ctx, provider)
    assert out.root_cause == "Container exits on startup."


def test_diagnose_bad_json_raises(mocker):
    ctx = load_fixture("crash_loop")
    provider = mocker.MagicMock()
    provider.complete.return_value = "not valid json at all {{{"
    with pytest.raises(DiagnosisError, match="Invalid diagnosis response"):
        engine.diagnose(ctx, provider)
    assert provider.complete.call_count == 2


def test_diagnose_retries_on_bad_json(mocker):
    provider = mocker.MagicMock()
    provider.complete.side_effect = ["not json <<<", json.dumps(_fake_result_dict())]
    ctx = load_fixture("crash_loop")
    result = engine.diagnose(ctx, provider)
    assert provider.complete.call_count == 2
    assert provider.complete.call_args_list[1][0][0] == RETRY_SYSTEM_PROMPT
    assert isinstance(result, DiagnosisResult)


def test_diagnose_raises_after_two_failures(mocker):
    provider = mocker.MagicMock()
    provider.complete.return_value = "still not json"
    ctx = load_fixture("crash_loop")
    with pytest.raises(DiagnosisError):
        engine.diagnose(ctx, provider)
    assert provider.complete.call_count == 2
