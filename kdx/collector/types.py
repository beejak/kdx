from datetime import datetime

from pydantic import BaseModel, ConfigDict


class K8sEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    timestamp: datetime
    reason: str
    message: str
    count: int
    source_component: str


class ContainerStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    ready: bool
    restart_count: int
    state: str
    reason: str | None
    exit_code: int | None
    last_state_reason: str | None
    last_exit_code: int | None


class ResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    cpu_request: str | None
    cpu_limit: str | None
    memory_request: str | None
    memory_limit: str | None


class PodSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    pod_name: str
    phase: str
    node_name: str | None
    conditions: list[dict]
    container_statuses: list[ContainerStatus]
    resource_limits: dict[str, ResourceLimits]
    events: list[K8sEvent]
    logs: str | None
    previous_logs: str | None


class DeploymentSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    namespace: str
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    conditions: list[dict]
    image: str
    selector: dict[str, str]


class DiagnosisContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    collected_at: datetime
    cluster_name: str
    namespace: str
    deployment: DeploymentSignal
    pods: list[PodSignal]
    namespace_events: list[K8sEvent]
    failure_class: str
    mock: bool = False


class DiagnosisResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    failure_class: str
    root_cause: str
    evidence: list[str]
    fix_command: str
    fix_explanation: str
    confidence: str


class DiagnosisError(Exception):
    pass
