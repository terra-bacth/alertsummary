# Metric catalogues

One file per cluster, named `<cluster-key>.txt` to match the key in `clusters.yaml` — so
`lab-cluster-1.txt` for the lab. One metric name per line; `#` comments and blank lines ignored.

The PromQL agent uses this in two ways:

1. **Grounding** — the names relevant to the current goals are shown in the prompt, so the model
   picks from what exists instead of guessing.
2. **Rejection** — a generated query naming a metric that is not in this file is *not executed*.
   Prometheus answers a query for a non-existent metric with an empty result, which is
   indistinguishable from a healthy system; without this check a hallucinated metric name reads
   as evidence that nothing is wrong.

If no catalogue file exists for a cluster, both behaviours are simply off — queries run
unvalidated, exactly as before. Nothing breaks; you just lose the guardrail.

## Generating one

In-cluster, against STF:

```bash
TOKEN=$(oc whoami -t)
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://<prometheus-host>/api/v1/label/__name__/values" \
  | jq -r '.data[]' | sort > lab-cluster-1.txt
```

Trim it if it is enormous — the file is a filter, so anything you leave out becomes a metric the
agent may not use. Keep at least the namespaces your alerting rules query (`collectd_*`,
`ceilometer_*`, `sensubility_*`), plus `up` and `ALERTS`.

Refresh it when you add exporters. A stale catalogue rejects valid queries, and the report says
so explicitly (`NOT EXECUTED: metric(s) not present on this Prometheus`), so the failure is at
least visible rather than silent.
