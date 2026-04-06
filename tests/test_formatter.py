from kdx.collector.mock import load_fixture
from kdx.collector.types import DiagnosisResult
from kdx.output.formatter import print_result


def test_print_result_smoke():
    ctx = load_fixture("crash_loop")
    result = DiagnosisResult(
        failure_class="CrashLoopBackOff",
        root_cause="Container exits repeatedly.",
        evidence=["[pod] crash-demo-abc123 restartCount=12"],
        fix_command="kubectl logs crash-demo-abc123 -n kdx-test -c crasher",
        fix_explanation="Verify application startup requirements.",
        confidence="high",
    )
    print_result(ctx, result)
