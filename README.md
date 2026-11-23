# k8s-rollout-stall-detector

[![ci](https://github.com/moveeeax/k8s-rollout-stall-detector/actions/workflows/ci.yml/badge.svg)](https://github.com/moveeeax/k8s-rollout-stall-detector/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Finds Deployments, StatefulSets and DaemonSets that are mid-rollout and not finishing,
and prints the concrete reason each one is stuck.

`kubectl rollout status` blocks and says `Waiting for deployment "x" rollout to finish`.
It never says the new ReplicaSet cannot be scheduled, or the image tag has a typo, or the
readiness probe never passes. Finding that out means `kubectl describe` on the workload,
then the ReplicaSet, then each pod. This does it in one command.

```
NAMESPACE      KIND        NAME          DESIRED  UPDATED  READY  AGE  REASON            DETAIL
billing        Deployment  billing-api   2        1        2      22m  ImagePullBackOff  billing-api-6d7e8f9a0-cc333: Back-off pulling image "ghcr.io/acme/bil...
observability  DaemonSet   log-shipper   4        3        3      44m  CrashLoopBackOff  log-shipper-dd444: back-off 2m40s restarting failed container=app pod...
payments       Deployment  checkout-api  3        1        3      22m  Unschedulable     checkout-api-9b7d5f3c2-zz901: 0/6 nodes are available: 2 Insufficient...

3 stalled rollouts (DaemonSet 1, Deployment 2)
```

## Install

```sh
pip install git+https://github.com/moveeeax/k8s-rollout-stall-detector.git
```

Python 3.11+, no runtime dependencies. For live clusters it shells out to `kubectl`, which
means your existing kubeconfig, context and RBAC apply unchanged.

## Usage

```sh
# every namespace in the current context
k8s-rollout-stall-detector --all-namespaces

# one namespace, only rollouts stuck for more than five minutes
k8s-rollout-stall-detector -n payments --min-age 300

# offline: save a dump during an incident, analyse it later
kubectl get deploy,sts,ds,pods -A -o json > dump.json
k8s-rollout-stall-detector --from-file dump.json --json
```

Exit codes are the contract, so it works as a deploy gate or a cron check:

| code | meaning |
| ---- | ------- |
| `0`  | no stalled rollouts |
| `1`  | at least one stalled rollout |
| `2`  | usage or input error |

```sh
k8s-rollout-stall-detector -n payments --min-age 300 || exit 1
```

### Flags

| flag | what it does |
| ---- | ------------ |
| `--from-file PATH` | read a JSON dump instead of the cluster; repeatable, `-` reads stdin |
| `-n`, `--namespace NS` | limit to one namespace |
| `-A`, `--all-namespaces` | look at every namespace |
| `--min-age SECONDS` | ignore rollouts younger than this (default `60`) |
| `--json` | emit a JSON array, one object per stalled workload |
| `--wide` | do not truncate the `DETAIL` column |
| `--quiet` | findings only, no summary line |
| `--now RFC3339` | treat this instant as now, for dumps saved earlier |
| `--kubectl BIN` | kubectl binary to call |
| `--context NAME` | kubectl context to read from |

## How it works

Two separate questions, answered separately.

**Is it stuck?** Pure arithmetic over `status`, per kind. A Deployment is mid-rollout when
`updatedReplicas` or `readyReplicas` is below `spec.replicas`, or when surge pods from the old
revision are still around. A StatefulSet adds the `currentRevision` vs `updateRevision` split,
and respects `rollingUpdate.partition` so a deliberately partitioned update is not reported.
A DaemonSet compares `numberReady` and `updatedNumberScheduled` against
`desiredNumberScheduled`. An `observedGeneration` behind `metadata.generation` counts as stuck
on its own: the controller has not even looked at your new spec.

**Why is it stuck?** Pods matching the workload's label selector are classified, and the most
actionable reason wins:

| reason | detected from |
| ------ | ------------- |
| `Paused` | `spec.paused` on the Deployment |
| `ImagePullBackOff` | container waiting state, including `ErrImagePull` and `InvalidImageName` |
| `CrashLoopBackOff` | container waiting state, with the last exit code and reason |
| `PendingVolume` | `PodScheduled=False` mentioning an unbound PersistentVolumeClaim |
| `Unschedulable` | `PodScheduled=False` with the scheduler's own message |
| `ReadinessProbeFailing` | container running, not `ready`, no restarts to explain it |
| `ProgressDeadlineExceeded` | the Deployment `Progressing` condition |
| `Unknown` | anything else, with the replica counters in the message |

Rollout age comes from the Deployment `Progressing` condition's `lastUpdateTime`. StatefulSets
and DaemonSets carry no such condition, so the newest pod the controller created is used
instead, which is the closest honest answer available from a dump.

## Offline mode

Pod ownership is resolved through the label selector, not through ReplicaSet owner references.
That costs one extra API call fewer and, more usefully, means a saved dump containing only
workloads and pods is enough:

```sh
kubectl get deploy,sts,ds,pods -A -o json > /tmp/incident.json
k8s-rollout-stall-detector --from-file /tmp/incident.json --now 2026-11-23T12:00:00Z --wide
```

`--now` matters here: ages are computed against it, so an old dump still reports the ages the
cluster had when you captured it. `examples/cluster-dump.json` is a dump of that shape with
three stalled workloads and two healthy ones.

A dump without pods still works; the stall is reported with reason `Unknown` and a message
saying pods were not in the input, rather than a guess.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests run entirely over the JSON fixtures in `tests/fixtures/`, so no cluster is needed.

## License

MIT
