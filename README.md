# AlertSummarizationAgent (tsagent)

Multi-agent system that troubleshoots RHOSP alarms end-to-end:

```
AlertManager ──webhook──► Ingest API ──► Correlation Engine ──► LangGraph Orchestrator
                                                                 ├─ Planner Agent
                                                                 ├─ PromQL Agent ──► Prometheus (per-cluster)
                                                                 ├─ Decision Node
                                                                 ├─ Node Agent ──SSH──► Director ──► Overcloud nodes
                                                                 ├─ ↺ Reflection (self-correction)
                                                                 └─ Report Agent ──► RCA report + recommendations
```

- **LLMs:** local vLLM (Llama 3.2 8B / Qwen) or public providers (Gemini/OpenAI/Anthropic) via LiteLLM — config-switchable, no code change.
- **RHOSP access model:** per-cluster director node; common troubleshooting user → `stack` → `heat-admin@<node>`, with a read-only command allow-list.
- **Multi-cluster:** `config/clusters.yaml` maps each RHOSP cluster to its Prometheus and director.
- **Deploy target:** Red Hat OpenShift (UBI9 image, Helm chart).

Full architecture & phased plan: [`.hermes/plans/2026-08-05_troubleshooting-agent-plan.md`](.hermes/plans/2026-08-05_troubleshooting-agent-plan.md)

## Dev setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
unset PYTHONPATH
pip install -e ".[agents,dev]"
pytest
uvicorn tsagent.main:app --reload --port 8080
```

## API

| Endpoint | Purpose |
|---|---|
| `POST /alerts` | AlertManager webhook receiver (returns 202 immediately) |
| `GET /alarms` | Recently ingested alarms |
| `GET /correlation/pending` | Groups currently buffered, and when each promotes |
| `POST /correlation/flush` | Promote every buffered group now (demos, drills) |
| `GET /incidents` | List incidents |
| `GET /incidents/{id}` | Incident with its alarms and evidence |
| `GET /incidents/{id}/report?format=md\|json` | RCA report |
| `GET /healthz`, `GET /readyz` | Probes |
| `GET /metrics` | Self-metrics for the ServiceMonitor |

## How a report gets made

1. **Ingest.** AlertManager posts to `/alerts`. Alarms are normalized, resolved to a cluster from their labels, persisted, and buffered. The webhook returns at once — an investigation takes minutes, and AlertManager would time out and redeliver.
2. **Correlate.** Alarms group by cluster + node (then site, then cluster), deduped by fingerprint. A group promotes to an incident once it has been quiet for `window_seconds` **or** open for `max_group_age_seconds` — the age cap exists so a continuously alarming node cannot defer its own investigation forever. A group whose alarms all resolve inside the window raises nothing.
3. **Investigate.** The LangGraph state machine runs plan → PromQL → reflect → decide → (node SSH → reflect) → report. Queries are scoped with the cluster's label matchers so a shared Prometheus cannot answer with another cluster's metrics.
4. **Report.** Evidence tables are rendered from what was actually executed; only the root cause, recommendations and timeline come from the model. Reports are retrievable as Markdown or JSON, and optionally POSTed to `report.notify_webhook`.

## Configuration

| File | What it controls |
|---|---|
| `config/settings.yaml` | Correlation window, reflection budget, investigation limits |
| `config/llm.yaml` | Provider profiles, per-agent model selection, fallback chain |
| `config/clusters.yaml` | Cluster → Prometheus + director inventory |
| `config/ssh_allowlist.yaml` | The commands the Node Agent may run |
| `config/runbooks/*.yaml` | Per-alarm hints fed to the planner |

Every setting is overridable by environment variable using `TSAGENT_` and `__` for nesting, so a running pod can be nudged without editing a ConfigMap:

```bash
TSAGENT_CORRELATION__WINDOW_SECONDS=60
TSAGENT_REFLECTION__ENABLED=false
TSAGENT_LLM_PROFILE=anthropic
```

### Switching LLM provider

Change `default_profile` in `config/llm.yaml` (or set `TSAGENT_LLM_PROFILE`). Per-agent overrides let the planner and PromQL agents run on a stronger model than the high-volume reflection step.

`fallback_chain` is empty by default on purpose: incident context contains node names and log excerpts, so falling back from a local model to a public provider is an egress decision an operator should opt into explicitly.

## Safety model

The Node Agent is read-only by construction, not by instruction:

- The model **selects** a command; it never composes the string that reaches a shell. Proposals are matched against anchored regexes in `config/ssh_allowlist.yaml`, and shell operators (`;`, `|`, `&`, `$`, backtick, redirects) and `..` traversal are rejected outright.
- Node targets are validated against the live overcloud inventory read from the director — an alarm label naming an unknown host is refused before any SSH happens.
- A refused command is fed back to the agent as evidence, so it retries with something allowed instead of failing the incident.
- Remediation is only ever *recommended* in the report. There is no code path that restarts, stops or modifies anything.

Every query, command and refusal is persisted as evidence for audit.

## Degraded behaviour

The system is built to still produce something useful when parts are missing, and to say so:

| Failure | Behaviour |
|---|---|
| Planner LLM unreachable | Generic health sweep runs; caveat recorded |
| PromQL LLM unreachable | No metric evidence; caveat recorded |
| Decision LLM unreachable | Defaults to metrics-only — never sends an unreviewed agent onto production nodes |
| Reflection unreachable | Step accepted with a caveat rather than blocking |
| Report LLM unreachable | Report still renders with all collected evidence and an "undetermined" root cause |
| No Prometheus / no director for a cluster | Skipped with a caveat |
| Pod restart mid-investigation | Incident resumed from its LangGraph checkpoint on next start |

## Testing

```bash
pytest
ruff check src tests
```

`tests/test_scenarios.py` drives canned incidents (disk full, service crash-loop, node down, network flap, multi-node, and a model that tries to remediate) through the whole pipeline. The LLM is faked there, so those tests pin down the system's behaviour — routing, scoping, allow-list enforcement, what reaches the report — not the quality of a model's reasoning. Judging that needs the prompt-iteration work against real models.

## Deploying to OpenShift

```bash
podman build -t tsagent:0.1.0 .
helm upgrade --install tsagent deploy/helm/tsagent \
  -f deploy/helm/tsagent/values-vllm.yaml \
  --namespace tsagent --create-namespace
```

Presets: `values-vllm.yaml` (in-cluster vLLM), `values-ollama.yaml` (Ollama), `values-public-llm.yaml` (Gemini/OpenAI/Anthropic). Raw manifests for Helm-less clusters are in `deploy/openshift/`.

The chart ships one replica by design — the case store and checkpointer are SQLite on a RWO volume. Scaling out needs the queue + PostgreSQL step from the plan, not a bigger replica count.

Point AlertManager at the service:

```yaml
receivers:
  - name: tsagent
    webhook_configs:
      - url: http://tsagent.tsagent.svc:8080/alerts
        send_resolved: true
```

SSH keys and API keys come from a Secret. Prefer creating it out of band and setting `secrets.existingSecret` rather than putting keys in a values file.
