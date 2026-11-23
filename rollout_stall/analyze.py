"""Decide which workloads are stuck mid-rollout, and why.

Two independent questions are answered here. First, is the workload mid-rollout
and not finishing (pure arithmetic over ``status`` counters, per kind). Second,
what is blocking it (classification of the pods that match its selector). The
second half is the interesting one: the counters tell you *that* a rollout is
stuck, which every `kubectl rollout status` already does, while the pods tell you
*why*, which is the part that normally costs five more commands.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import POD_REASON_PRIORITY, Finding, PodProblem, Reason

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")

#: Waiting reasons that all mean "the kubelet cannot get the image".
IMAGE_WAITING_REASONS = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "ImageInspectError",
        "ErrImageNeverPull",
        "RegistryUnavailable",
    }
)

_WHITESPACE = re.compile(r"\s+")
_MAX_MESSAGE = 300


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse an RFC3339 Kubernetes timestamp into an aware datetime."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def _tidy(message: Optional[str]) -> str:
    if not message:
        return ""
    text = _WHITESPACE.sub(" ", str(message)).strip()
    if len(text) > _MAX_MESSAGE:
        text = text[: _MAX_MESSAGE - 3].rstrip() + "..."
    return text


def _int(source: Optional[Dict[str, Any]], key: str, default: int = 0) -> int:
    value = (source or {}).get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _conditions(obj: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = ((obj.get("status") or {}).get("conditions")) or []
    return {c.get("type"): c for c in raw if isinstance(c, dict) and c.get("type")}


def _namespace(obj: Dict[str, Any]) -> str:
    return ((obj.get("metadata") or {}).get("namespace")) or "default"


# --------------------------------------------------------------------------- #
# pod ownership
# --------------------------------------------------------------------------- #


def _selector_matches(pod: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not labels and not expressions:
        # An empty selector would match every pod in the namespace. Refuse.
        return False
    pod_labels = ((pod.get("metadata") or {}).get("labels")) or {}
    for key, want in labels.items():
        if pod_labels.get(key) != want:
            return False
    for expression in expressions:
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values") or []
        have = pod_labels.get(key)
        if operator == "In" and have not in values:
            return False
        if operator == "NotIn" and have in values:
            return False
        if operator == "Exists" and key not in pod_labels:
            return False
        if operator == "DoesNotExist" and key in pod_labels:
            return False
    return True


def owned_pods(workload: Dict[str, Any], pods: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pods in the workload's namespace that match its label selector.

    Matching by selector rather than by ReplicaSet owner reference is deliberate:
    it needs no extra API call and works on a dump that only contains workloads
    and pods, which is what people actually save during an incident.
    """
    namespace = _namespace(workload)
    selector = ((workload.get("spec") or {}).get("selector")) or {}
    return [
        pod
        for pod in pods
        if _namespace(pod) == namespace and _selector_matches(pod, selector)
    ]


# --------------------------------------------------------------------------- #
# pod classification
# --------------------------------------------------------------------------- #


def classify_pod(pod: Dict[str, Any], now: dt.datetime) -> Optional[PodProblem]:
    """Return why this pod is holding up a rollout, or None if it looks fine."""
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    name = meta.get("name") or "<unnamed>"
    phase = status.get("phase") or "Unknown"

    if meta.get("deletionTimestamp"):
        # Terminating pods are the previous revision going away, not a blocker.
        return None

    created = parse_time(meta.get("creationTimestamp"))
    age = (now - created).total_seconds() if created else None

    conditions = {
        c.get("type"): c
        for c in (status.get("conditions") or [])
        if isinstance(c, dict) and c.get("type")
    }
    init_statuses = status.get("initContainerStatuses") or []
    container_statuses = status.get("containerStatuses") or []
    all_statuses = list(init_statuses) + list(container_statuses)
    restarts = max([_int(c, "restartCount") for c in all_statuses] or [0])

    def problem(reason: str, message: Optional[str], image: Optional[str] = None) -> PodProblem:
        return PodProblem(
            name=name,
            phase=phase,
            reason=reason,
            message=_tidy(message),
            image=image,
            restarts=restarts,
            age_seconds=age,
        )

    scheduled = conditions.get("PodScheduled") or {}
    if scheduled.get("status") == "False":
        message = scheduled.get("message") or scheduled.get("reason") or "pod is not scheduled"
        # An unbound PVC surfaces as Unschedulable too; it needs a different fix,
        # so split it out before the generic capacity case.
        if "PersistentVolumeClaim" in message or "unbound" in message:
            return problem(Reason.PENDING_VOLUME, message)
        if scheduled.get("reason") == "Unschedulable":
            return problem(Reason.UNSCHEDULABLE, message)
        return problem(Reason.UNKNOWN, message)

    for container in all_statuses:
        waiting = ((container.get("state") or {}).get("waiting")) or {}
        if waiting.get("reason") in IMAGE_WAITING_REASONS:
            message = waiting.get("message") or "cannot pull image %s" % container.get("image")
            return problem(Reason.IMAGE_PULL, message, container.get("image"))

    for container in all_statuses:
        waiting = ((container.get("state") or {}).get("waiting")) or {}
        if waiting.get("reason") == "CrashLoopBackOff":
            terminated = ((container.get("lastState") or {}).get("terminated")) or {}
            message = waiting.get("message") or "container %s is crash looping" % container.get("name")
            detail = []
            if terminated.get("exitCode") is not None:
                detail.append("exit code %s" % terminated["exitCode"])
            if terminated.get("reason"):
                detail.append(str(terminated["reason"]))
            if detail:
                message = "%s (%s)" % (message, ", ".join(detail))
            return problem(Reason.CRASHLOOP, message, container.get("image"))

    for container in all_statuses:
        waiting = ((container.get("state") or {}).get("waiting")) or {}
        reason = waiting.get("reason")
        if reason in ("ContainerCreating", "PodInitializing"):
            message = waiting.get("message") or "stuck in %s" % reason
            if "volume" in message.lower():
                return problem(Reason.PENDING_VOLUME, message)
            return problem(
                Reason.UNKNOWN, "container %s is stuck in %s" % (container.get("name"), reason)
            )

    if container_statuses:
        not_ready = [c for c in container_statuses if not c.get("ready")]
        running_not_ready = [c for c in not_ready if ((c.get("state") or {}).get("running"))]
        if running_not_ready:
            ready_condition = conditions.get("Ready") or {}
            container = running_not_ready[0]
            message = ready_condition.get("message") or (
                "container %s is running but never became ready" % container.get("name")
            )
            return problem(Reason.READINESS, message, container.get("image"))
        if not_ready:
            return problem(
                Reason.UNKNOWN, "container %s is not ready" % not_ready[0].get("name")
            )

    if phase in ("Pending", "Unknown", "Failed"):
        return problem(Reason.UNKNOWN, status.get("message") or "pod phase is %s" % phase)

    ready_condition = conditions.get("Ready") or {}
    if ready_condition.get("status") == "False":
        return problem(Reason.READINESS, ready_condition.get("message") or "pod is not Ready")

    return None


# --------------------------------------------------------------------------- #
# per-kind rollout arithmetic
# --------------------------------------------------------------------------- #

#: (desired, updated, ready, observed, incomplete)
_State = Tuple[int, int, int, int, bool]


def _deployment_state(obj: Dict[str, Any]) -> _State:
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    desired = 1 if spec.get("replicas") is None else _int(spec, "replicas")
    updated = _int(status, "updatedReplicas")
    ready = _int(status, "readyReplicas")
    observed = _int(status, "replicas")
    # observed > desired means surge pods from the old revision are still around.
    incomplete = updated < desired or ready < desired or observed > desired
    return desired, updated, ready, observed, incomplete


def _statefulset_state(obj: Dict[str, Any]) -> _State:
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    desired = 1 if spec.get("replicas") is None else _int(spec, "replicas")
    rolling = ((spec.get("updateStrategy") or {}).get("rollingUpdate")) or {}
    partition = _int(rolling, "partition")
    # A partitioned update is *supposed* to leave the lower ordinals untouched.
    expected_updated = max(desired - partition, 0)
    updated = _int(status, "updatedReplicas")
    ready = _int(status, "readyReplicas")
    observed = _int(status, "replicas")
    revision_split = bool(status.get("updateRevision")) and (
        status.get("currentRevision") != status.get("updateRevision")
    )
    incomplete = (
        ready < desired
        or updated < expected_updated
        or (revision_split and partition == 0)
    )
    return desired, updated, ready, observed, incomplete


def _daemonset_state(obj: Dict[str, Any]) -> _State:
    status = obj.get("status") or {}
    desired = _int(status, "desiredNumberScheduled")
    ready = _int(status, "numberReady")
    updated = _int(status, "updatedNumberScheduled")
    observed = _int(status, "currentNumberScheduled")
    unavailable = _int(status, "numberUnavailable")
    incomplete = desired > 0 and (
        ready < desired or updated < desired or unavailable > 0
    )
    return desired, updated, ready, observed, incomplete


_STATE_BY_KIND = {
    "Deployment": _deployment_state,
    "StatefulSet": _statefulset_state,
    "DaemonSet": _daemonset_state,
}


def _generation_lag(obj: Dict[str, Any]) -> bool:
    """True when the controller has not even observed the latest spec yet."""
    generation = (obj.get("metadata") or {}).get("generation")
    observed = (obj.get("status") or {}).get("observedGeneration")
    if generation is None or observed is None:
        return False
    try:
        return int(observed) < int(generation)
    except (TypeError, ValueError):
        return False


def _rollout_start(
    obj: Dict[str, Any], pods: Sequence[Dict[str, Any]]
) -> Optional[dt.datetime]:
    """When this rollout last made progress.

    Deployments carry a Progressing condition whose lastUpdateTime is exactly
    that. StatefulSets and DaemonSets carry nothing useful, so the newest pod the
    controller created is the honest stand-in.
    """
    progressing = _conditions(obj).get("Progressing")
    if progressing:
        stamp = parse_time(progressing.get("lastUpdateTime")) or parse_time(
            progressing.get("lastTransitionTime")
        )
        if stamp:
            return stamp
    newest = None
    for pod in pods:
        stamp = parse_time(((pod.get("metadata") or {}).get("creationTimestamp")))
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    if newest:
        return newest
    return parse_time(((obj.get("metadata") or {}).get("creationTimestamp")))


def _pick_reason(
    obj: Dict[str, Any],
    problems: Sequence[PodProblem],
    paused: bool,
    desired: int,
    updated: int,
    ready: int,
    had_pods: bool,
) -> Tuple[str, str]:
    progressing = _conditions(obj).get("Progressing") or {}
    deadline_exceeded = (
        progressing.get("status") == "False"
        and progressing.get("reason") == "ProgressDeadlineExceeded"
    )

    if paused:
        return Reason.PAUSED, "rollout is paused; resume it with kubectl rollout resume"

    if problems:
        top = problems[0]
        detail = "%s: %s" % (top.name, top.message) if top.message else top.name
        if top.reason == Reason.IMAGE_PULL and top.image and top.image not in detail:
            detail = "%s (image %s)" % (detail, top.image)
        if deadline_exceeded:
            detail = "%s [progress deadline exceeded]" % detail
        return top.reason, detail

    if deadline_exceeded:
        return Reason.PROGRESS_DEADLINE, _tidy(
            progressing.get("message") or "progress deadline exceeded"
        )

    counters = "updated %d/%d, ready %d/%d" % (updated, desired, ready, desired)
    if not had_pods:
        return Reason.UNKNOWN, "no pods in the input, so no reason to report (%s)" % counters
    return Reason.UNKNOWN, "no unhealthy pod matched the selector (%s)" % counters


def count_workloads(items: Iterable[Dict[str, Any]]) -> int:
    """How many Deployments, StatefulSets and DaemonSets were examined."""
    return sum(1 for item in items if (item.get("kind") or "") in WORKLOAD_KINDS)


def analyze(
    items: Sequence[Dict[str, Any]],
    now: Optional[dt.datetime] = None,
    min_age: float = 0.0,
) -> List[Finding]:
    """Return one Finding per stalled workload, sorted for stable output."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    pods = [item for item in items if (item.get("kind") or "") == "Pod"]
    findings: List[Finding] = []

    for obj in items:
        kind = obj.get("kind") or ""
        state_fn = _STATE_BY_KIND.get(kind)
        if state_fn is None:
            continue
        desired, updated, ready, observed, incomplete = state_fn(obj)
        if _generation_lag(obj):
            incomplete = True
        if not incomplete:
            continue

        mine = owned_pods(obj, pods)
        start = _rollout_start(obj, mine)
        age = (now - start).total_seconds() if start else 0.0
        if age < min_age:
            continue

        problems = [p for p in (classify_pod(pod, now) for pod in mine) if p is not None]
        problems.sort(
            key=lambda p: (
                POD_REASON_PRIORITY.index(p.reason)
                if p.reason in POD_REASON_PRIORITY
                else len(POD_REASON_PRIORITY),
                -(p.age_seconds or 0.0),
            )
        )
        paused = bool((obj.get("spec") or {}).get("paused"))
        reason, message = _pick_reason(
            obj, problems, paused, desired, updated, ready, bool(pods)
        )

        findings.append(
            Finding(
                kind=kind,
                namespace=_namespace(obj),
                name=((obj.get("metadata") or {}).get("name")) or "<unnamed>",
                desired=desired,
                updated=updated,
                ready=ready,
                observed=observed,
                reason=reason,
                message=message,
                age_seconds=max(age, 0.0),
                paused=paused,
                pods=problems,
            )
        )

    findings.sort(key=lambda f: (f.namespace, f.kind, f.name))
    return findings
