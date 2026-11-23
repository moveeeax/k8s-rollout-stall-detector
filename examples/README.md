# examples

`cluster-dump.json` is the shape `kubectl get deploy,sts,ds,pods -A -o json` produces: a
`kind: List` of mixed objects. It describes a cluster with five workloads, three of them stuck:

- `payments/checkout-api` — the new ReplicaSet has nowhere to schedule
- `billing/billing-api` — the release tag `2.4.1-rc3` does not exist in the registry
- `observability/log-shipper` — one node keeps OOM killing the agent
- `data/postgres` and `web/storefront` — healthy, and stay out of the report

Timestamps are anchored around `2026-11-23T12:00:00Z`, so pass `--now` to see the ages the
dump was captured with:

```sh
k8s-rollout-stall-detector --from-file examples/cluster-dump.json --now 2026-11-23T12:00:00Z
```

Without `--now` the ages grow against the real clock, which is also what you want when the
dump is fresh.
