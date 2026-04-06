import pytest

from kdx.collector.k8s import _classify_failure
from kdx.collector.mock import list_fixtures, load_fixture
from kdx.collector.types import ContainerStatus, PodSignal


def _pod(container_statuses: list[ContainerStatus]) -> PodSignal:
    return PodSignal(
        pod_name="test-pod",
        phase="Running",
        node_name="node-1",
        conditions=[],
        container_statuses=container_statuses,
        resource_limits={},
        events=[],
        logs=None,
        previous_logs=None,
    )


def _cs(**kwargs) -> ContainerStatus:
    defaults = dict(
        name="app", ready=False, restart_count=0,
        state="waiting", reason=None, exit_code=None,
        last_state_reason=None, last_exit_code=None,
    )
    return ContainerStatus(**{**defaults, **kwargs})


def test_all_fixtures_are_valid():
    for name in list_fixtures():
        ctx = load_fixture(name)
        assert ctx.failure_class in (
            "CrashLoopBackOff",
            "OOMKilled",
            "ImagePullBackOff",
            "Pending",
            "Unknown",
        )


def test_load_fixture_missing_raises():
    with pytest.raises(FileNotFoundError, match="No fixture"):
        load_fixture("does_not_exist_ever")


def test_classify_oom_killed():
    pods = [_pod([_cs(reason="OOMKilled")])]
    assert _classify_failure(pods) == "OOMKilled"


def test_classify_oom_from_last_state():
    pods = [_pod([_cs(last_state_reason="OOMKilled")])]
    assert _classify_failure(pods) == "OOMKilled"


def test_classify_crash_loop():
    pods = [_pod([_cs(reason="CrashLoopBackOff")])]
    assert _classify_failure(pods) == "CrashLoopBackOff"


def test_classify_image_pull_backoff():
    pods = [_pod([_cs(reason="ImagePullBackOff")])]
    assert _classify_failure(pods) == "ImagePullBackOff"


def test_classify_err_image_pull():
    pods = [_pod([_cs(reason="ErrImagePull")])]
    assert _classify_failure(pods) == "ImagePullBackOff"


def test_classify_pending():
    pods = [_pod([])]
    pods[0] = pods[0].model_copy(update={"phase": "Pending"})
    assert _classify_failure(pods) == "Pending"


def test_classify_unknown():
    pods = [_pod([_cs(reason="SomeOtherReason")])]
    assert _classify_failure(pods) == "Unknown"


def test_classify_oom_beats_crash_loop():
    # OOMKilled has higher priority than CrashLoopBackOff
    pods = [_pod([_cs(reason="CrashLoopBackOff", last_state_reason="OOMKilled")])]
    assert _classify_failure(pods) == "OOMKilled"


def test_classify_empty_pods():
    assert _classify_failure([]) == "Unknown"
