"""Findings and the vocabulary used to describe a stalled rollout."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Reason:
    """Why a rollout is stuck.

    These are the only reasons the detector emits. Everything it cannot pin on a
    concrete pod state becomes ``Unknown`` with the counters in the message, so a
    finding is never silently dropped.
    """

    PAUSED = "Paused"
    IMAGE_PULL = "ImagePullBackOff"
    CRASHLOOP = "CrashLoopBackOff"
    PENDING_VOLUME = "PendingVolume"
    UNSCHEDULABLE = "Unschedulable"
    READINESS = "ReadinessProbeFailing"
    PROGRESS_DEADLINE = "ProgressDeadlineExceeded"
    UNKNOWN = "Unknown"


#: Pod-derived reasons, most actionable first. A pod that cannot pull its image
#: explains a stall better than the same pod also failing its readiness probe.
POD_REASON_PRIORITY = (
    Reason.IMAGE_PULL,
    Reason.CRASHLOOP,
    Reason.PENDING_VOLUME,
    Reason.UNSCHEDULABLE,
    Reason.READINESS,
    Reason.UNKNOWN,
)


def human_duration(seconds: float) -> str:
    """Render a duration the way kubectl does: 45s, 12m30s, 3h5m, 2d4h."""
    total = int(max(seconds, 0))
    if total < 60:
        return "%ds" % total
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "%dm%ds" % (minutes, secs) if secs else "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd%dh" % (days, hours) if hours else "%dd" % days


@dataclass
class PodProblem:
    """One unhealthy pod behind a stalled rollout."""

    name: str
    phase: str
    reason: str
    message: str = ""
    image: Optional[str] = None
    restarts: int = 0
    age_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "phase": self.phase,
            "reason": self.reason,
            "message": self.message,
            "restarts": self.restarts,
        }
        if self.image:
            out["image"] = self.image
        if self.age_seconds is not None:
            out["age"] = human_duration(self.age_seconds)
        return out


@dataclass
class Finding:
    """A workload that is mid-rollout and not getting any further."""

    kind: str
    namespace: str
    name: str
    desired: int
    updated: int
    ready: int
    observed: int
    reason: str
    message: str = ""
    age_seconds: float = 0.0
    paused: bool = False
    pods: List[PodProblem] = field(default_factory=list)

    @property
    def target(self) -> str:
        """The `kubectl`-style reference, e.g. ``deployment/checkout-api``."""
        return "%s/%s" % (self.kind.lower(), self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "kind": self.kind,
            "name": self.name,
            "target": self.target,
            "reason": self.reason,
            "message": self.message,
            "desiredReplicas": self.desired,
            "updatedReplicas": self.updated,
            "readyReplicas": self.ready,
            "observedReplicas": self.observed,
            "rolloutAgeSeconds": int(self.age_seconds),
            "rolloutAge": human_duration(self.age_seconds),
            "paused": self.paused,
            "pods": [pod.to_dict() for pod in self.pods],
        }
