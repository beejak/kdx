from kdx.collector.mock import load_fixture
from kdx.diagnosis.prompts import build_context_message


def test_build_context_message_preclassification():
    ctx = load_fixture("crash_loop")
    msg = build_context_message(ctx)
    assert msg.startswith("PRE-CLASSIFICATION: CrashLoopBackOff")


def test_build_context_message_truncates_long_logs():
    ctx = load_fixture("crash_loop")
    long_log = "\n".join(f"line {i}" for i in range(70))
    pod = ctx.pods[0]
    new_pod = pod.model_copy(update={"logs": long_log})
    ctx2 = ctx.model_copy(update={"pods": [new_pod]})
    msg = build_context_message(ctx2)
    assert "[... " in msg
    assert "lines omitted" in msg
