from datetime import UTC, datetime, timedelta

from kubernetes import client, config
from kubernetes.client import ApiException

from kdx.collector.types import (
    ContainerStatus,
    DeploymentSignal,
    DiagnosisContext,
    K8sEvent,
    PodSignal,
    ResourceLimits,
)


def _load_kube_config(kube_context: str | None) -> str:
    try:
        config.load_kube_config(context=kube_context)
        contexts, active = config.list_kube_config_contexts()
        return active["name"]
    except config.ConfigException:
        config.load_incluster_config()
        return "in-cluster"


def _classify_failure(pods: list[PodSignal]) -> str:
    for pod in pods:
        for cs in pod.container_statuses:
            if cs.reason == "OOMKilled" or cs.last_state_reason == "OOMKilled":
                return "OOMKilled"
    for pod in pods:
        for cs in pod.container_statuses:
            if cs.reason == "CrashLoopBackOff":
                return "CrashLoopBackOff"
    for pod in pods:
        for cs in pod.container_statuses:
            if cs.reason in ("ErrImagePull", "ImagePullBackOff"):
                return "ImagePullBackOff"
    if pods and all(p.phase == "Pending" for p in pods):
        return "Pending"
    return "Unknown"


def _label_selector(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _parse_event_time(ev) -> datetime:
    ts = ev.event_time or ev.last_timestamp or ev.first_timestamp
    if ts is None:
        return datetime.now(UTC)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _k8s_event(ev) -> K8sEvent:
    sc = ""
    if ev.source:
        sc = ev.source.component or ""
        if ev.source.host:
            sc = f"{sc}/{ev.source.host}" if sc else (ev.source.host or "")
    return K8sEvent(
        timestamp=_parse_event_time(ev),
        reason=ev.reason or "",
        message=ev.message or "",
        count=ev.count or 1,
        source_component=sc or "unknown",
    )


def _container_status(st) -> ContainerStatus:
    name = st.name or ""
    ready = bool(st.ready)
    restart_count = st.restart_count or 0
    reason = None
    exit_code = None
    state_str = "waiting"
    last_state_reason = None
    last_exit_code = None
    if st.state:
        if st.state.waiting:
            state_str = "waiting"
            reason = st.state.waiting.reason
        elif st.state.running:
            state_str = "running"
        elif st.state.terminated:
            state_str = "terminated"
            reason = st.state.terminated.reason
            exit_code = st.state.terminated.exit_code
        if st.last_state and st.last_state.terminated:
            last_state_reason = st.last_state.terminated.reason
            last_exit_code = st.last_state.terminated.exit_code
    return ContainerStatus(
        name=name,
        ready=ready,
        restart_count=restart_count,
        state=state_str,
        reason=reason,
        exit_code=exit_code,
        last_state_reason=last_state_reason,
        last_exit_code=last_exit_code,
    )


def _resource_limits(container) -> ResourceLimits:
    r = container.resources
    req = (r.requests or {}) if r else {}
    lim = (r.limits or {}) if r else {}
    return ResourceLimits(
        cpu_request=req.get("cpu"),
        cpu_limit=lim.get("cpu"),
        memory_request=req.get("memory"),
        memory_limit=lim.get("memory"),
    )


def _failing_container_name(pod_v1) -> str:
    specs = pod_v1.spec.containers if pod_v1.spec else []
    statuses = pod_v1.status.container_statuses if pod_v1.status else None
    if statuses:
        for st in statuses:
            if st.name and not st.ready:
                return st.name
    return specs[0].name if specs else ""


def collect(deployment: str, namespace: str, kube_context: str | None) -> DiagnosisContext:
    cluster_name = _load_kube_config(kube_context)
    k8s_config = client.Configuration.get_default_copy()
    k8s_config.socket_options = None
    api_client = client.ApiClient(configuration=k8s_config)
    api_client.configuration.timeout = 10
    apps = client.AppsV1Api(api_client=api_client)
    core = client.CoreV1Api(api_client=api_client)
    try:
        dep = apps.read_namespaced_deployment(deployment, namespace)
    except ApiException as e:
        if e.status == 403:
            raise RuntimeError(
                f"kdx needs read access to Deployments, Pods, Events in namespace {namespace}. Check RBAC."
            ) from e
        raise
    match_labels = dep.spec.selector.match_labels or {}
    selector_str = _label_selector(match_labels)
    tmpl = dep.spec.template
    spec_containers = tmpl.spec.containers if tmpl and tmpl.spec else []
    first_image = spec_containers[0].image if spec_containers else ""
    dep_conditions: list[dict] = []
    for c in dep.status.conditions or []:
        dep_conditions.append(
            {
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            }
        )
    dep_sig = DeploymentSignal(
        name=dep.metadata.name or deployment,
        namespace=dep.metadata.namespace or namespace,
        desired_replicas=dep.spec.replicas or 0,
        ready_replicas=dep.status.ready_replicas or 0,
        available_replicas=dep.status.available_replicas or 0,
        conditions=dep_conditions,
        image=first_image,
        selector=dict(match_labels),
    )
    try:
        pod_list = core.list_namespaced_pod(namespace, label_selector=selector_str or None)
    except ApiException as e:
        if e.status == 403:
            raise RuntimeError(
                f"kdx needs read access to Deployments, Pods, Events in namespace {namespace}. Check RBAC."
            ) from e
        raise
    pod_items = (pod_list.items or [])[:5]
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=30)
    try:
        ns_events_raw = core.list_namespaced_event(namespace)
    except ApiException as e:
        if e.status == 403:
            raise RuntimeError(
                f"kdx needs read access to Deployments, Pods, Events in namespace {namespace}. Check RBAC."
            ) from e
        raise
    ns_events_list: list[K8sEvent] = []
    for ev in ns_events_raw.items or []:
        ts = _utc(_parse_event_time(ev))
        if ts >= cutoff:
            ns_events_list.append(_k8s_event(ev))
    ns_events_list.sort(key=lambda e: e.timestamp, reverse=True)
    ns_events_list = ns_events_list[:50]
    pods_signals: list[PodSignal] = []
    for pod in pod_items:
        pod_name = pod.metadata.name or ""
        phase = pod.status.phase or "Unknown"
        node_name = pod.spec.node_name if pod.spec and pod.spec.node_name else None
        pod_conds: list[dict] = []
        for c in pod.status.conditions or []:
            pod_conds.append(
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                }
            )
        csts = [_container_status(st) for st in (pod.status.container_statuses or [])]
        reslim: dict[str, ResourceLimits] = {}
        for c in pod.spec.containers or []:
            if c.name:
                reslim[c.name] = _resource_limits(c)
        try:
            pe = core.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
            )
        except ApiException:
            pe = type("E", (), {"items": []})()
        pe_list = [_k8s_event(ev) for ev in (pe.items or [])]
        pe_list.sort(key=lambda e: e.timestamp, reverse=True)
        pe_list = pe_list[:50]
        failing = _failing_container_name(pod)
        logs = None
        prev_logs = None
        restart_count = 0
        for st in pod.status.container_statuses or []:
            if st.name == failing:
                restart_count = st.restart_count or 0
                break
        if phase != "Pending" and failing:
            try:
                logs = core.read_namespaced_pod_log(
                    pod_name,
                    namespace,
                    container=failing,
                    tail_lines=100,
                )
            except ApiException:
                logs = None
            if restart_count > 0:
                try:
                    prev_logs = core.read_namespaced_pod_log(
                        pod_name,
                        namespace,
                        container=failing,
                        previous=True,
                        tail_lines=50,
                    )
                except ApiException:
                    prev_logs = None
        pods_signals.append(
            PodSignal(
                pod_name=pod_name,
                phase=phase,
                node_name=node_name,
                conditions=pod_conds,
                container_statuses=csts,
                resource_limits=reslim,
                events=pe_list,
                logs=logs,
                previous_logs=prev_logs,
            )
        )
    fc = _classify_failure(pods_signals)
    return DiagnosisContext(
        collected_at=now,
        cluster_name=cluster_name,
        namespace=namespace,
        deployment=dep_sig,
        pods=pods_signals,
        namespace_events=ns_events_list,
        failure_class=fc,
        mock=False,
    )
