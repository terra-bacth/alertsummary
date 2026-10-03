# Running tsagent on OpenShift

One image, one pod. The API, the correlation engine, the investigation graph and the dashboard all
run in a single container. There's exactly **one replica** because the case store and checkpointer
are SQLite on a RWO volume and the SSE event bus is in-process.

Nothing environment-specific is baked into the code or the image. Everything comes from:

| Source | Holds | OpenShift object |
|---|---|---|
| `config/*.yaml` | cluster inventory, LLM profiles, tunables, SSH allow-list, runbooks, PromQL library | ConfigMaps (`tsagent-config`, `-allowlist`, `-runbooks`, `-knowledge`) |
| `TSAGENT_*` env vars | process settings + per-key overrides of `settings.yaml` | ConfigMap `tsagent-env` (via `envFrom`) |
| credentials | SSH key, known_hosts, director password, Prometheus token, LLM API keys | Secret |

```
┌───────────── OpenShift namespace: tsagent ───────────────────────────────────────┐
│  Route (edge | reencrypt, 1h timeout for SSE) ──► Service :8080 ──► Pod          │
│                                                     ├─ /etc/tsagent/config  ◄── ConfigMaps
│  AlertManager ──POST /alerts──────────────────────► │  /etc/tsagent/ssh     ◄── Secret
│  Prometheus ──scrape /metrics (ServiceMonitor)────► │  /opt/app-root/data   ◄── PVC (SQLite)
│                                                     └─ env                  ◄── ConfigMap + Secret
│  NetworkPolicy: egress only to DNS, Prometheus, director SSH, LLM endpoints      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Get an image

> **Agent naming:** Each agent is named after a One Piece character whose role mirrors their function.

**Build inside OpenShift** (no local container tooling needed). With Helm, set
`build.enabled=true`. With Kustomize, the `lab` overlay already includes `components/build`:

```bash
oc start-build tsagent -n tsagent --follow
```

The Deployment carries an `image.openshift.io/triggers` annotation, so it rolls automatically
whenever a build pushes a new image. For a private repo, set `build.git.sourceSecret`.

**Or build and push yourself:**

```bash
podman build -t quay.io/acme/tsagent:0.1.0 .
podman push quay.io/acme/tsagent:0.1.0
# helm: --set image.repository=quay.io/acme/tsagent --set image.tag=0.1.0
```

**Disconnected clusters:** every external location is a build arg (`PYTHON_IMAGE`, `NODE_IMAGE`,
`PIP_INDEX_URL`, `PIP_TRUSTED_HOST`, `NPM_REGISTRY`). Pass them to `podman build --build-arg`,
or set them in `build.args` in the chart. The image sets `LITELLM_LOCAL_MODEL_COST_MAP=True`, so
nothing is fetched from the internet at runtime.

## 2. Create the Secret (out of band)

```bash
oc new-project tsagent

# Helm (one Secret, referenced by secrets.existingSecret):
oc create secret generic tsagent-secrets \
  --from-file=sshPrivateKey=$HOME/.ssh/tsagent_ed25519 \
  --from-file=sshKnownHosts=./known_hosts \
  --from-literal=TSAGENT_DIRECTOR_PASSWORD='...' \
  --from-literal=TSAGENT_PROM_TOKEN="$(oc create token prometheus-reader -n service-telemetry --duration=8760h)"

# Kustomize (SSH material and env vars are separate Secrets):
oc create secret generic tsagent-ssh \
  --from-file=sshPrivateKey=$HOME/.ssh/tsagent_ed25519 --from-file=sshKnownHosts=./known_hosts
oc create secret generic tsagent-env-secrets \
  --from-literal=TSAGENT_DIRECTOR_PASSWORD='...' --from-literal=ANTHROPIC_API_KEY='...'
```

Build a `known_hosts` file with `ssh-keyscan -t ed25519 <director-host> > known_hosts`. Verify it
out of band, since this file *is* the trust decision. Without it the pod still works, but it logs a
warning on every connection because host keys are not checked.

Every key is optional. Without an SSH key, investigations are metrics-only. Without a
Prometheus token, the pod falls back to `bearer_token_file`.

## 3a. Deploy with Helm

```bash
# Lab: the repo's config/ as-is, image built in-cluster
helm upgrade --install tsagent deploy/helm/tsagent -n tsagent \
  --set build.enabled=true --set secrets.existingSecret=tsagent-secrets

# Production: hardening preset + LLM preset + your site file
helm upgrade --install tsagent deploy/helm/tsagent -n tsagent \
  -f deploy/helm/tsagent/values-openshift-prod.yaml \
  -f deploy/helm/tsagent/values-vllm.yaml \
  -f my-site.yaml

helm test tsagent -n tsagent   # checks /readyz through the Service
```

| Preset | Purpose |
|---|---|
| `values-openshift-prod.yaml` | service-CA TLS + reencrypt Route, known_hosts verification, JSON logs, demo off, trusted CA bundle, Prometheus RBAC |
| `values-vllm.yaml` / `values-ollama.yaml` / `values-public-llm.yaml` | LLM backends |

`deploy/helm/tsagent/values.yaml` documents every value. The main groups:

- **image / build**: registry, tag or digest, pull secrets, and the BuildConfig (git uri/ref/contextDir, build args, webhooks, auto-deploy).
- **workload**: resources, probes (liveness/readiness/startup), security contexts, nodeSelector/tolerations/affinity/topology spread, priority class, `hostAliases` (for directors that aren't in cluster DNS), dnsConfig, extra volumes/containers/env/envFrom.
- **networking**: Service, Route (host, path, TLS, annotations such as `ip_whitelist`, shard labels), service-serving-cert TLS, NetworkPolicy (ingress namespaces, egress ports and peers), ServiceMonitor.
- **trust / egress**: `proxy.*` (HTTP(S)_PROXY/NO_PROXY), `trustedCABundle.enabled` (OpenShift's injected cluster CA bundle).
- **identity**: ServiceAccount, `rbac.prometheusReader` (binds `cluster-monitoring-view`, or a namespaced Role, so `bearer_token_file` works).
- **persistence**: size, storage class, access mode, existing claim, keep-on-uninstall.
- **config**: `settings` (deep-merged), `llm` / `clusters` (replace), `allowlist.content`, `runbooks.extra`, `knowledge.metrics`, or `existingConfigMap` for any of them.
- **env**: any `TSAGENT_*` variable, rendered to the `tsagent-env` ConfigMap.

### Where chart defaults come from

The chart's defaults **are** the repo's `config/` files, copied into `deploy/helm/tsagent/files/`
because Helm can only package files inside the chart. After editing anything in `config/`, run:

```bash
scripts/sync-chart-config.sh          # copy
scripts/sync-chart-config.sh --check  # CI: fail on drift (tests/test_config.py checks this too)
```

The script copies only named files, never keys.

## 3b. Deploy with Kustomize (no Helm)

The ConfigMaps are generated **directly from `config/`**, so this path never drifts. Because
`config/` sits outside the kustomization directory, you need the load-restrictor flag:

```bash
oc kustomize --load-restrictor LoadRestrictionsNone deploy/openshift/overlays/lab  | oc apply -f -
oc kustomize --load-restrictor LoadRestrictionsNone deploy/openshift/overlays/prod | oc apply -f -
```

```
deploy/openshift/
├── base/                 SA, PVC, Deployment, Service, Route, NetworkPolicy, ServiceMonitor,
│   └── env.properties    TSAGENT_* process settings → tsagent-env ConfigMap
├── components/
│   ├── build/            ImageStream + BuildConfig + image-change trigger
│   ├── service-tls/      service-CA certificate, HTTPS probes, reencrypt Route
│   └── trusted-ca/       cluster CA bundle injection
└── overlays/
    ├── lab/              base + build
    └── prod/             base + service-tls + trusted-ca, env overrides, 10Gi PVC
```

Kustomize appends a content hash to generated ConfigMap names, so any config edit rolls the pod.

## 4. Point AlertManager at it

| AlertManager | Where the receiver goes |
|---|---|
| Platform (`openshift-monitoring`) | `alertmanager-main` Secret, or *Administration → Cluster Settings → Alertmanager* |
| User-workload monitoring | an `AlertmanagerConfig` in the tsagent namespace |
| STF (`service-telemetry`) | the `alertmanager-default` Secret; also add `service-telemetry` to `networkPolicy.ingressFromNamespaces` |

```yaml
receivers:
  - name: tsagent
    webhook_configs:
      - url: http://tsagent-tsagent.tsagent.svc:8080/alerts   # `helm status` prints the exact URL
        send_resolved: true
```

With `tls.serviceServingCert.enabled`, use `https://` and trust the service CA
(`/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt`).

---

## Configuration reference

### Layering

```
built-in default  <  config/*.yaml (ConfigMap)  <  TSAGENT_* env var
```

- `settings.yaml`: every key can be overridden as `TSAGENT_<SECTION>__<KEY>`, e.g.
  `TSAGENT_CORRELATION__WINDOW_SECONDS=60` or `TSAGENT_DEMO__ENABLED=false`. You can nudge a
  running pod without touching a ConfigMap: `oc set env deploy/tsagent TSAGENT_REFLECTION__ENABLED=false`.
- **`${ENV}` placeholders** work in any string in `settings.yaml`, `clusters.yaml` and `llm.yaml`.
  They are resolved in the pod after the YAML is parsed, so a value can never inject structure:

  ```yaml
  prometheus_instances:
    stf:
      url: "${PROMETHEUS_URL:-https://prometheus-default.service-telemetry.svc:9092}"
      timeout_seconds: "${PROMETHEUS_TIMEOUT:-30}"
  profiles:
    qwen:
      api_base: "${QWEN_API_BASE}"
  ```

  | Form | Result |
  |---|---|
  | `${VAR}` | value, or empty string plus a warning if unset |
  | `${VAR:-default}` | default when VAR is unset **or empty** |
  | `${VAR-default}` | default only when VAR is unset |
  | `$${VAR}` | the literal text `${VAR}` |

  The SSH allow-list is **never** expanded, because it's the security boundary. PromQL's `$1` is
  left untouched.

### Process settings (env only)

These are needed before any file is read. The chart manages the ones marked †.

| Variable | Default | Purpose |
|---|---|---|
| `TSAGENT_CONFIG_DIR` † | `config` | directory holding the YAML files |
| `TSAGENT_DB_PATH` † | `data/cases.db` | case store (SQLite) |
| `TSAGENT_CHECKPOINT_PATH` † | `data/checkpoints.db` | LangGraph checkpoints |
| `TSAGENT_UI_DIR` | image: `/opt/app-root/src/web/dist` | serve the dashboard; empty disables it |
| `TSAGENT_HOST` | `::` | bind address (`0.0.0.0` where the pod network has no IPv6) |
| `TSAGENT_PORT` † | `8080` | listen port |
| `TSAGENT_ROOT_PATH` | `` | path prefix when the Route uses `path:` |
| `TSAGENT_PROXY_HEADERS` / `TSAGENT_FORWARDED_ALLOW_IPS` | `true` / `*` | trust the router's X-Forwarded-* headers |
| `TSAGENT_TIMEOUT_KEEP_ALIVE` | `5` | HTTP keep-alive (s) |
| `TSAGENT_TIMEOUT_GRACEFUL_SHUTDOWN` | `20` | drain time on SIGTERM (s) |
| `TSAGENT_SSL_CERTFILE` / `TSAGENT_SSL_KEYFILE` † | `` | serve HTTPS (service-serving cert) |
| `TSAGENT_CORS_ORIGINS` | `*` | comma-separated; irrelevant when the UI is same-origin |
| `TSAGENT_METRICS_ENABLED` | `true` | `/metrics` |
| `TSAGENT_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`… |
| `TSAGENT_LOG_FORMAT` | `text` | `json` for OpenShift Logging / Loki |
| `TSAGENT_ACCESS_LOG` | `true` | per-request logs |
| `TSAGENT_LLM_PROFILE` | – | overrides `default_profile` in `llm.yaml` |

### `settings.yaml` sections

| Section | Keys |
|---|---|
| `correlation` | `window_seconds`, `max_group_age_seconds`, `sweep_interval_seconds` |
| `reflection` | `enabled`, `max_retries` |
| `investigation` | `max_promql_queries`, `max_node_commands`, `node_access_threshold` |
| `report` | `notify_webhook`, `notify_timeout_seconds`, `notify_token_env`, `max_tokens`, `evidence_items`, `evidence_max_chars`, `evidence_result_chars` |
| `ssh` | `connect_timeout_seconds`, `command_timeout_seconds`, `keepalive_interval_seconds`, `known_hosts`, `output_max_chars` |
| `events` | `buffer_size`, `subscriber_queue_size`, `stream_replay` |
| `orchestrator` | `shutdown_grace_seconds`, `resume_on_start` |
| `demo` | `enabled`, `step_pause_seconds` |
| `api` | `overview_incidents`, `overview_alarms` |

Per-cluster settings (Prometheus URL/TLS/token, director host/user/auth/known_hosts/OSP version,
label matchers) live in `clusters.yaml`. LLM endpoints, models, timeouts and fallbacks live in
`llm.yaml`.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Pod `CrashLoopBackOff`, `bind: address family not supported` | no IPv6 in the pod network: set `TSAGENT_HOST=0.0.0.0` |
| Every Prometheus query "unreachable", TLS error | set `ca_bundle` to `/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt` (in-cluster) or enable `trustedCABundle` |
| HTTP 401/403 from Prometheus | token missing or lacks RBAC: `rbac.prometheusReader.enabled=true` with `bearer_token_file`, or set `TSAGENT_PROM_TOKEN` |
| SSH timeouts | NetworkPolicy egress (port 22), or the director isn't resolvable: use `hostAliases` |
| `Host key is not trusted` | `known_hosts` doesn't contain the director's key: re-scan and update the Secret |
| Dashboard reconnects every 30s | Route timeout annotation missing (`haproxy.router.openshift.io/timeout: 1h`) |
| Config edit ignored | Helm and Kustomize both roll on change; if you edited a ConfigMap by hand, run `oc rollout restart deploy/tsagent` |
| `Multi-Attach error` on upgrade | expected with RWO + `Recreate`; the old pod must terminate first |
