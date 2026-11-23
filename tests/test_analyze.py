"""Detection and classification over the JSON fixtures."""

import datetime as dt

from rollout_stall.analyze import analyze, classify_pod, count_workloads, owned_pods, parse_time
from rollout_stall.model import Reason

from conftest import NOW, load_fixture


def only(findings):
    assert len(findings) == 1, "expected exactly one finding, got %r" % [
        (f.target, f.reason) for f in findings
    ]
    return findings[0]


def test_healthy_cluster_has_no_findings():
    items = load_fixture("healthy.json")
    assert analyze(items, now=NOW) == []
    assert count_workloads(items) == 3


def test_unschedulable_deployment_is_reported_with_the_scheduler_message():
    finding = only(analyze(load_fixture("deploy-unschedulable.json"), now=NOW))
    assert finding.kind == "Deployment"
    assert finding.namespace == "payments"
    assert finding.name == "checkout-api"
    assert finding.reason == Reason.UNSCHEDULABLE
    assert "Insufficient cpu" in finding.message
    assert "checkout-api-9b7d5f3c2-zz901" in finding.message
    assert (finding.desired, finding.updated, finding.ready) == (3, 1, 3)
    # the Progressing condition was last updated at 11:38, fixtures pin now at 12:00
    assert 1300 < finding.age_seconds < 1400


def test_image_pull_names_the_failing_image():
    finding = only(analyze(load_fixture("deploy-imagepull.json"), now=NOW))
    assert finding.reason == Reason.IMAGE_PULL
    assert "ghcr.io/acme/billing-api:2.4.1-rc3" in finding.message
    assert finding.pods[0].image == "ghcr.io/acme/billing-api:2.4.1-rc3"


def test_crashloop_reports_the_exit_code():
    finding = only(analyze(load_fixture("deploy-crashloop.json"), now=NOW))
    assert finding.reason == Reason.CRASHLOOP
    assert "exit code 1" in finding.message
    assert finding.pods[0].restarts == 8


def test_readiness_failure_is_not_confused_with_a_crash():
    finding = only(analyze(load_fixture("deploy-readiness.json"), now=NOW))
    assert finding.reason == Reason.READINESS
    assert finding.pods[0].phase == "Running"
    assert finding.pods[0].restarts == 0


def test_statefulset_unbound_pvc_is_pending_volume_not_unschedulable():
    finding = only(analyze(load_fixture("sts-pending-volume.json"), now=NOW))
    assert finding.kind == "StatefulSet"
    assert finding.reason == Reason.PENDING_VOLUME
    assert "PersistentVolumeClaims" in finding.message
    assert finding.pods[0].name == "kafka-2"


def test_daemonset_stall_uses_the_newest_pod_as_the_rollout_clock():
    finding = only(analyze(load_fixture("ds-stalled.json"), now=NOW))
    assert finding.kind == "DaemonSet"
    assert finding.reason == Reason.CRASHLOOP
    assert "OOMKilled" in finding.message
    assert (finding.desired, finding.updated, finding.ready) == (4, 3, 3)
    # newest pod was created at 11:16
    assert 2600 < finding.age_seconds < 2700


def test_progress_deadline_without_pods_in_the_input():
    finding = only(analyze(load_fixture("deploy-progress-deadline.json"), now=NOW))
    assert finding.reason == Reason.PROGRESS_DEADLINE
    assert "timed out progressing" in finding.message


def test_paused_rollout_beats_every_other_reason():
    finding = only(analyze(load_fixture("deploy-paused.json"), now=NOW))
    assert finding.reason == Reason.PAUSED
    assert finding.paused is True
    assert "rollout resume" in finding.message


def test_workloads_only_input_says_so_instead_of_guessing():
    finding = only(analyze(load_fixture("workloads-only.json"), now=NOW))
    assert finding.reason == Reason.UNKNOWN
    assert "no pods in the input" in finding.message
    assert "updated 1/3" in finding.message


def test_min_age_skips_young_rollouts():
    items = load_fixture("deploy-unschedulable.json")
    assert analyze(items, now=NOW, min_age=60) != []
    assert analyze(items, now=NOW, min_age=3600) == []


def test_example_dump_reports_three_stalls_and_skips_the_healthy_pair():
    import json
    import os

    from conftest import EXAMPLES
    from rollout_stall.collect import flatten

    with open(os.path.join(EXAMPLES, "cluster-dump.json"), "r", encoding="utf-8") as handle:
        items = flatten(json.load(handle))
    findings = analyze(items, now=NOW, min_age=60)
    assert sorted(f.target for f in findings) == [
        "daemonset/log-shipper",
        "deployment/billing-api",
        "deployment/checkout-api",
    ]
    assert count_workloads(items) == 5


# --------------------------------------------------------------------------- #
# edge cases that are cheaper to express inline than as a fixture file
# --------------------------------------------------------------------------- #


def deployment(desired, updated, ready, replicas, **extra):
    spec = {"replicas": desired, "selector": {"matchLabels": {"app": "x"}}}
    spec.update(extra.pop("spec", {}))
    obj = {
        "kind": "Deployment",
        "metadata": {
            "name": "x",
            "namespace": "ns",
            "generation": 2,
            "creationTimestamp": "2026-11-01T00:00:00Z",
        },
        "spec": spec,
        "status": {
            "observedGeneration": 2,
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
        },
    }
    obj["status"].update(extra.pop("status", {}))
    return obj


def test_scaled_to_zero_is_not_a_stall():
    assert analyze([deployment(0, 0, 0, 0)], now=NOW) == []


def test_unobserved_generation_counts_as_a_stall():
    obj = deployment(2, 2, 2, 2, status={"observedGeneration": 1})
    finding = only(analyze([obj], now=NOW))
    assert finding.reason == Reason.UNKNOWN


def test_surge_pods_still_around_count_as_an_unfinished_rollout():
    finding = only(analyze([deployment(2, 2, 2, 3)], now=NOW))
    assert finding.observed == 3


def test_partitioned_statefulset_update_is_not_a_stall():
    sts = {
        "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "ns", "generation": 1},
        "spec": {
            "replicas": 5,
            "selector": {"matchLabels": {"app": "db"}},
            "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"partition": 3}},
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 5,
            "readyReplicas": 5,
            "updatedReplicas": 2,
            "currentRevision": "db-1",
            "updateRevision": "db-2",
        },
    }
    assert analyze([sts], now=NOW) == []


def test_terminating_pods_are_not_blamed_for_the_stall():
    pod = {
        "kind": "Pod",
        "metadata": {
            "name": "old",
            "namespace": "ns",
            "labels": {"app": "x"},
            "creationTimestamp": "2026-11-23T11:00:00Z",
            "deletionTimestamp": "2026-11-23T11:59:00Z",
        },
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]},
    }
    assert classify_pod(pod, NOW) is None
    finding = only(analyze([deployment(2, 1, 1, 2), pod], now=NOW))
    assert finding.reason == Reason.UNKNOWN
    assert "no unhealthy pod matched" in finding.message


def test_empty_selector_never_adopts_pods():
    obj = deployment(2, 1, 1, 2, spec={"selector": {}})
    pod = {
        "kind": "Pod",
        "metadata": {"name": "p", "namespace": "ns", "labels": {"app": "x"}},
        "status": {"phase": "Pending"},
    }
    assert owned_pods(obj, [pod]) == []


def test_match_expressions_are_honoured():
    obj = deployment(1, 0, 0, 1, spec={
        "selector": {
            "matchLabels": {"app": "x"},
            "matchExpressions": [{"key": "tier", "operator": "In", "values": ["web"]}],
        }
    })
    web = {
        "kind": "Pod",
        "metadata": {"name": "web", "namespace": "ns", "labels": {"app": "x", "tier": "web"}},
        "status": {"phase": "Pending"},
    }
    batch = {
        "kind": "Pod",
        "metadata": {"name": "batch", "namespace": "ns", "labels": {"app": "x", "tier": "batch"}},
        "status": {"phase": "Pending"},
    }
    assert [p["metadata"]["name"] for p in owned_pods(obj, [web, batch])] == ["web"]


def test_pods_in_another_namespace_are_ignored():
    obj = deployment(1, 0, 0, 1)
    pod = {
        "kind": "Pod",
        "metadata": {"name": "p", "namespace": "other", "labels": {"app": "x"}},
        "status": {"phase": "Pending"},
    }
    assert owned_pods(obj, [pod]) == []


def test_parse_time_accepts_z_and_offsets_and_rejects_junk():
    assert parse_time("2026-11-23T12:00:00Z") == dt.datetime(
        2026, 11, 23, 12, 0, tzinfo=dt.timezone.utc
    )
    assert parse_time("2026-11-23T15:00:00+03:00").utctimetuple().tm_hour == 12
    assert parse_time("not a time") is None
    assert parse_time(None) is None
