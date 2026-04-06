import json
from pathlib import Path

from click.testing import CliRunner

from kdx.cli import cli
from kdx.collector.types import DiagnosisResult


def _fake_result() -> DiagnosisResult:
    return DiagnosisResult(
        failure_class="CrashLoopBackOff",
        root_cause="Container exits on startup.",
        evidence=["[pod] crash-demo restart loop"],
        fix_command="kubectl logs deploy/crash-demo -n kdx-test",
        fix_explanation="Inspect logs.",
        confidence="high",
    )


def _patch_engine(mocker):
    mocker.patch("kdx.diagnosis.engine.diagnose", return_value=_fake_result())


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_diagnose_mock_runs(mocker):
    _patch_engine(mocker)
    runner = CliRunner()
    result = runner.invoke(cli, ["diagnose", "crash-demo", "--mock", "crash_loop"])
    assert result.exit_code == 0


def test_diagnose_mock_bad_fixture():
    runner = CliRunner()
    result = runner.invoke(cli, ["diagnose", "crash-demo", "--mock", "no_such_fixture"])
    assert result.exit_code == 2


def test_diagnose_mock_dump_context(mocker, tmp_path):
    _patch_engine(mocker)
    out = tmp_path / "ctx.json"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["diagnose", "crash-demo", "--mock", "crash_loop", "--dump-context", str(out)]
    )
    assert result.exit_code == 0
    data = json.loads(out.read_text())
    assert data["failure_class"] == "CrashLoopBackOff"


def test_diagnose_missing_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["diagnose", "crash-demo", "--mock", "crash_loop"])
    assert result.exit_code == 2


def test_diagnose_all_mock_fixtures(mocker):
    _patch_engine(mocker)
    runner = CliRunner()
    for fixture in ("crash_loop", "oom_kill", "image_pull_backoff", "pending_unschedulable"):
        result = runner.invoke(cli, ["diagnose", "demo", "--mock", fixture])
        assert result.exit_code == 0, f"{fixture}: {result.output}"
