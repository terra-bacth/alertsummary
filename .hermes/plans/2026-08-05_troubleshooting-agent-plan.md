# Multi-Agent Alarm Troubleshooting System — Architecture & Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a multi-agent system that ingests alarms from AlertManager, correlates them, investigates via Prometheus (PromQL) and node SSH login, self-reflects at each stage, and produces an analysis report with remediation recommendations. Deployable on Red Hat OpenShift, working with local LLMs (vLLM/Ollama) and public LLMs (Gemini, OpenAI, Claude).

**Architecture:** Event-driven pipeline of specialized agents orchestrated by a LangGraph state machine. AlertManager pushes alarms via webhook → correlation window groups them → an investigation graph (PromQL agent → decision → Node/SSH agent) runs with reflection checkpoints → report agent emits findings. All LLM calls go through a single provider-abstraction layer (LiteLLM) so switching vLLM ↔ Ollama ↔ Gemini/GPT/Claude is pure config.

**Tech Stack:** Python 3.11, FastAPI, LangGraph (+ LangChain core), LiteLLM, prometheus-api-client, asyncssh, SQLite (case store), Pydantic v2, Jinja2 (reports), Docker/Podman, Helm chart + OpenShift manifests.

---

## 1. Architecture

### 1.1 High-level component diagram

```
                          ┌────────────────────────────────────────────────┐
                          │           Troubleshooting Agent System         │
 ┌──────────────┐  POST   │  ┌──────────────┐      ┌─────────────────────┐ │
 │ AlertManager ├────────►│  │ Ingest API   │─────►│ Correlation Engine  │ │
 │  (webhook)   │ /alerts │  │ (FastAPI)    │      │ (time/node/site     │ │
 └──────────────┘         │  └──────────────┘      │  grouping, buffer)  │ │
                          │                        └──────────┬──────────┘ │
                          │                                   │ Incident   │
                          │                    ┌──────────────▼──────────┐ │
                          │                    │  Orchestrator (LangGraph)│ │
                          │                    │  ┌─────────────────────┐ │ │
                          │                    │  │ 1. Planner Agent    │ │ │
                          │                    │  │ 2. PromQL Agent ────┼─┼─┼──► Prometheus API
                          │                    │  │ 3. Decision Node    │ │ │
                          │                    │  │ 4. Node Agent (SSH)─┼─┼─┼──► Nodes (logs,
                          │                    │  │ 5. Report Agent     │ │ │     systemctl…)
                          │                    │  │ ↺ Reflection loop   │ │ │
                          │                    │  └─────────────────────┘ │ │
                          │                    └──────────┬──────────────┘ │
                          │   ┌──────────────┐            │                │
                          │   │ LLM Gateway  │◄───────────┘                │
                          │   │ (LiteLLM)    │  Case Store (SQLite)        │
                          │   └──┬───┬───┬───┘  Report out (MD/JSON/API)   │
                          └──────┼───┼───┼─────────────────────────────────┘
                                 │   │   │
                        vLLM/Ollama  │  Gemini / OpenAI / Claude
                        (OpenShift)  └── (public, via API keys)
```

### 1.2 Agents & responsibilities

| Agent | Role | Tools it uses |
|---|---|---|
| **Correlation Engine** (deterministic + LLM assist) | Buffer alarms for a window (default 5 min); group by node/site/labels; dedupe; promote group → *Incident* | Rules first; LLM only for ambiguous grouping |
| **Planner Agent** | Reads the incident, produces an investigation plan (which metrics to check, hypotheses) | LLM + alarm-runbook knowledge base |
| **PromQL Agent** | Generates PromQL from plan, executes against Prometheus, interprets results | `prometheus_query` tool (range + instant) |
| **Decision Node** | Deterministic + LLM: based on metric evidence, decide "sufficient → report" vs "need node access" | Graph conditional edge |
| **Node Agent** | SSH into node; run whitelisted read-only commands (journalctl, systemctl status, df, top, dmesg, tail of service logs); summarize findings | `ssh_exec` tool with command allow-list |
| **Reflection Agent** (cross-cutting) | After each agent step: critique output (is PromQL valid? does evidence support conclusion? did we miss an alarm?), request retry with feedback (max 2 retries/step) | LLM (can be cheaper model) |
| **Report Agent** | Final RCA report: timeline, correlated alarms, evidence (queries + results + log excerpts), root-cause hypothesis, recommended corrective steps, confidence score | Jinja2 template → Markdown/JSON |

### 1.3 Orchestration model (LangGraph)

Single `StateGraph` with a shared typed state (`IncidentState`):

```
ingest → correlate → plan → promql_investigate → reflect₁
   reflect₁ ─(retry)→ promql_investigate
   reflect₁ ─(ok)→ decide
decide ─(metrics sufficient)→ report
decide ─(need node)→ node_investigate → reflect₂ ─(ok)→ report
                                        reflect₂ ─(retry)→ node_investigate
report → END  (persist case, notify)
```

- **State** carries: raw alarms, incident, plan, executed queries + results, node command outputs, reflection notes, retry counters.
- **Checkpointing:** LangGraph SQLite checkpointer → resumable incidents, full audit trail.
- **Why LangGraph over CrewAI/AutoGen:** explicit graph = deterministic control over reflection loops and retry limits; first-class state persistence; runs fine in one container (simplest OpenShift deploy). Agents communicate via shared state, not free-form chat — more reliable for ops automation.

### 1.4 LLM abstraction

One `llm/gateway.py` wrapping **LiteLLM**:

```yaml
# config/llm.yaml  (lab reality: Llama 3.2 8B Instruct + Qwen on CPU-only vLLM, ~60 vCPU)
default_profile: llama-lab
profiles:
  llama-lab: { model: "hosted_vllm/meta-llama/Llama-3.2-8B-Instruct", api_base: "http://<vllm-llama>:8000/v1" }
  qwen-lab:  { model: "hosted_vllm/Qwen/<model-id>", api_base: "http://<vllm-qwen>:8000/v1" }
  gemini:    { model: "gemini/gemini-2.5-pro" }
  openai:    { model: "gpt-4o" }
  anthropic: { model: "claude-sonnet-4-5" }
per_agent_overrides:
  planner: qwen-lab        # try Qwen for planning/PromQL (often stronger at structured output)
  reflection: llama-lab
```

**CPU-inference constraints (design consequences, applied across all agents):**
- Token discipline: short system prompts, summarize evidence before feeding it back to the LLM (never dump raw query results/logs into context), cap generation with `max_tokens`.
- Generous HTTP timeouts (120–300 s) + async: the graph awaits LLM calls without blocking ingest.
- Reflection is optional per step via config (`reflection.enabled`, default on) — first knob to turn if latency hurts; POC volume is low so multi-minute incident processing is acceptable.
- Structured output via constrained JSON (vLLM guided_json / low temperature) to avoid parse-retry loops that double CPU cost.
- Benchmark both lab models per agent role in Phase 7; fall back to a public provider profile if 8B quality/latency is insufficient for PromQL generation.

Switching providers = env var / ConfigMap change, zero code change. Fallback chain (local → public) on provider failure.

### 1.5 Security & safety

- Node Agent: **read-only command allow-list** (regex-validated), no shell interpolation of LLM output, SSH keys from OpenShift Secrets, per-node credentials mapped in config.
- LLM never executes commands directly — it selects from tool schemas; tools validate args.
- All prompts/responses + commands logged to case store (audit).
- NetworkPolicies restrict egress to Prometheus, nodes' SSH, LLM endpoints.

### 1.6 Data flow / persistence

- `cases.db` (SQLite, PVC-backed): incidents, steps, evidence, reports. (Swap to PostgreSQL later if scale demands — repository pattern keeps it isolated.)
- Reports exposed via `GET /incidents/{id}/report` (JSON + Markdown) and optionally pushed to webhook/Slack/email.

---

## 2. Repository layout

```
Troubleshooting_Agent/
├── IDEA.md
├── pyproject.toml
├── config/
│   ├── settings.yaml          # correlation window, retry limits, prometheus url
│   ├── llm.yaml               # provider profiles
│   ├── ssh_allowlist.yaml     # permitted node commands
│   └── runbooks/              # optional per-alarm-type hints (md/yaml)
├── src/tsagent/
│   ├── main.py                # FastAPI app entry
│   ├── api/routes.py          # /alerts webhook, /incidents endpoints
│   ├── models/                # Pydantic: Alarm, Incident, Evidence, Report
│   ├── correlation/engine.py
│   ├── graph/
│   │   ├── state.py           # IncidentState
│   │   ├── builder.py         # LangGraph wiring
│   │   └── nodes/             # planner.py, promql.py, decide.py, node_ssh.py,
│   │                          # reflect.py, report.py
│   ├── tools/
│   │   ├── prometheus.py      # query/query_range wrapper
│   │   └── ssh.py             # asyncssh + allow-list enforcement
│   ├── llm/gateway.py         # LiteLLM wrapper + profiles + fallback
│   ├── store/repository.py    # SQLite case store
│   └── reporting/             # templates/report.md.j2, renderer.py
├── tests/                     # pytest, mirrors src layout
├── Dockerfile                 # UBI9 python base (OpenShift-friendly, non-root)
└── deploy/
    ├── helm/tsagent/          # chart: Deployment, Service, Route, ConfigMaps,
    │                          # Secrets, PVC, NetworkPolicy, ServiceMonitor
    └── openshift/             # raw manifests alternative
```

---

## 3. Implementation phases (step-by-step)

### Phase 0 — Scaffold & foundations (½ day)
1. `uv venv` (remember: `unset PYTHONPATH` first on this machine), `pyproject.toml` with deps below.
2. Pydantic models: `Alarm` (parse AlertManager webhook payload v4), `Incident`, `Evidence`, `Report`.
3. FastAPI app with `POST /alerts` that validates + stores alarms; health probes `/healthz`, `/readyz`.
4. SQLite repository + tests.
   ✅ Verify: `pytest`, `curl -X POST /alerts` with a sample AlertManager payload.

### Phase 1 — Correlation engine (1 day)
1. Sliding-window buffer (asyncio task): collect alarms for `correlation_window_seconds` (default 300).
2. Grouping rules: same `node`/`instance` label → group; same `site` label → group; identical fingerprint → dedupe; still-pending (unresolved) alarms for a node join the group.
3. Emit `Incident` into orchestrator queue.
4. LLM-assisted grouping only for leftovers (optional flag, off by default).
   ✅ Verify: unit tests feeding synthetic alarm bursts (mixed nodes/sites) → expected incident groupings.

### Phase 2 — LLM gateway (½ day)
1. `llm/gateway.py`: `complete(agent_name, messages, tools=None)` via LiteLLM, profile resolution, retries, fallback chain, token/latency logging.
2. Config-driven profiles (vLLM OpenAI-compatible endpoint, Ollama, Gemini, OpenAI, Anthropic).
   ✅ Verify: smoke test against Ollama locally (`ollama run llama3.1`) and one public provider.

### Phase 3 — LangGraph orchestrator core (1–2 days)
1. `IncidentState` TypedDict + SQLite checkpointer.
2. **Planner node**: prompt with incident + runbook snippets → structured `InvestigationPlan` (Pydantic-validated JSON output; retry on parse fail).
3. **PromQL node**: resolves the incident's cluster → its Prometheus URL from `clusters.yaml`; for each plan item → generate PromQL (auto-injecting the cluster's label matchers) → validate syntax (dry-run against that Prometheus `/api/v1/query` with `timeout`) → execute → attach result summary to evidence.
4. **Reflection node** (generic, reused): critique prompt: *"Given goal X and output Y, is it correct/sufficient? Answer VERDICT: OK|RETRY + feedback."* Max 2 retries per step, then proceed with a noted caveat (never infinite-loop).
5. **Decision node**: rubric (deterministic thresholds where possible + LLM judgment) → `sufficient | need_node_access`.
   ✅ Verify: run graph against a **mock Prometheus** (pytest fixture with canned responses) end-to-end to decision.

### Phase 4 — Node (SSH) agent, RHOSP director-hop model (1–1.5 days)

**Access model (RHOSP):** the agent never SSHes to overcloud nodes directly. Per cluster there is a **director node** (undercloud). Flow:

```
Agent pod ──SSH (common troubleshooting user)──► Director node (per cluster)
              └─ su/sudo → stack user
                    └─ ssh heat-admin@<overcloud-node-ctlplane-ip>   (stack's key)
                          └─ run read-only diagnostic commands
```

1. `config/clusters.yaml` — **single source of truth per cluster**: director host, troubleshoot user, and the Prometheus that monitors it (in prod one Prometheus serves 2–3 RHOSP clusters; in lab it's 1:1):

```yaml
# config/clusters.yaml
prometheus_instances:
  prom-lab:
    url: "http://<lab-prometheus>:9090"
clusters:
  lab-cluster-1:                # matches alarm label: cluster / site
    prometheus: prom-lab        # which Prometheus monitors this cluster
    director:
      host: "<director-fqdn-or-ip>"
      user: "troubleshoot"      # common troubleshooting user
    labels: { site: "lab" }     # extra label matchers for alarm→cluster resolution
# prod: add more prometheus_instances + clusters; several clusters may share one prometheus
```

- Resolution flow: alarm labels → cluster entry → (a) Prometheus URL for the PromQL agent, (b) director for the Node agent. The PromQL agent scopes queries with the cluster's label matchers so a shared Prometheus returns only that cluster's metrics.
- Node-name → ctlplane IP resolved dynamically on the director (`source stackrc && openstack server list` / `metalsmith list`).
2. `tools/ssh.py`: asyncssh to director; command execution wrapped as
   `sudo -u stack ssh -o BatchMode=yes heat-admin@<node> '<allow-listed command>'`.
   - Allow-list validated **before** wrapping — LLM output never reaches the shell unvalidated; node target validated against the resolved inventory.
   - Director-level commands also allow-listed (e.g. `source stackrc; openstack server list`, `openstack baremetal node list`, pcs status via node).
3. RHOSP-aware allow-list additions: `sudo podman ps` (OSP16+ services are containers), `pcs status` (controllers), `ovs-vsctl show`, `systemctl status tripleo_*`, `journalctl -u tripleo_*`, `tail /var/log/containers/...`.
4. Node agent loop: LLM proposes next command (tool call) → validate → execute via director hop → summarize → repeat (max N=8) → findings.
5. Reflection checkpoint after node investigation.
   ✅ Verify: tests against two chained Docker sshd containers (fake director + fake node) with seeded fake logs; assert allow-list blocks `rm`, pipes, unlisted nodes.

### Phase 5 — Report agent & API (½–1 day)
1. Jinja2 Markdown template: summary, alarm timeline, correlation rationale, evidence tables (query → result), node findings, root-cause hypothesis, **recommended corrective actions**, confidence, caveats from reflection.
2. Persist report; `GET /incidents`, `GET /incidents/{id}`, `GET /incidents/{id}/report?format=md|json`.
3. Optional notifier (webhook URL from config).
   ✅ Verify: golden-file test for report rendering; full E2E: webhook alarms → report retrieved via API.

### Phase 6 — Containerization & OpenShift (1 day)
1. Dockerfile: `registry.access.redhat.com/ubi9/python-311`, non-root, arbitrary-UID-safe (OpenShift SCC `restricted-v2`), read-only rootfs except /tmp + PVC.
2. Helm chart: Deployment (1 replica; SQLite = RWO PVC), Service, Route, ConfigMaps (settings/llm/allowlist), Secret (API keys, SSH keys), NetworkPolicy, ServiceMonitor (expose own /metrics via prometheus-fastapi-instrumentator), liveness/readiness probes.
3. Values presets: `values-vllm.yaml`, `values-ollama.yaml`, `values-public-llm.yaml`.
4. AlertManager side: document `receiver` webhook config pointing at the Route/Service.
   ✅ Verify: `podman build` + local run; `helm template` lint; deploy to a test namespace (`oc apply`), fire a synthetic alert from AlertManager.

### Phase 7 — Hardening & evaluation (ongoing)
- Scenario test-suite: 5–10 canned incident scenarios (disk full, service crash-loop, node down, network flap) with mocked Prometheus/SSH → assert report identifies correct root cause.
- Prompt iteration per agent; per-agent model selection tuning.
- Optional later: PostgreSQL, horizontal scaling with a queue (Redis), web UI, feedback loop that stores operator verdicts to improve prompts/runbooks.

---

## 4. Dependencies

### Python packages
| Package | Purpose |
|---|---|
| `fastapi`, `uvicorn[standard]` | Webhook ingest + API |
| `pydantic>=2`, `pydantic-settings` | Models, config |
| `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-core` | Orchestration, state, checkpointing |
| `litellm` | Unified LLM API (vLLM/Ollama/Gemini/OpenAI/Anthropic) |
| `prometheus-api-client` (or plain `httpx`) | PromQL execution |
| `asyncssh` | Node SSH access |
| `jinja2` | Report templates |
| `aiosqlite` | Case store |
| `prometheus-fastapi-instrumentator` | Self-metrics |
| `tenacity` | Retries |
| dev: `pytest`, `pytest-asyncio`, `respx` (HTTP mocking), `ruff` | Testing/lint |

### External/infra dependencies
- **AlertManager** ≥0.25 (webhook receiver config → our Route)
- **Prometheus** — per-cluster mapping in `config/clusters.yaml` (prod: one Prometheus monitors 2–3 RHOSP clusters; each cluster entry points at its Prometheus). Lab POC: single cluster + single Prometheus, reachable from the pod.
- **LLM backend** — at least one of: vLLM (OpenAI-compatible `/v1`) on OpenShift · Ollama on OpenShift · API keys for Gemini/OpenAI/Anthropic (stored as Secrets)
- **SSH access (RHOSP model)** — per-cluster director node reachable from the pod; common troubleshooting user on each director (key as OpenShift Secret); that user can switch to `stack`, and `stack` holds keys to `heat-admin@<overcloud nodes>` (standard TripleO setup — already in place)
- **Cluster inventory config** — mapping of alarm site/cluster labels → director hostnames (`config/clusters.yaml`)
- **OpenShift** 4.12+: namespace, RWO PVC storage class, Route, permission to run restricted-v2 SCC (design already complies)
- Local dev: Python 3.11, podman/docker, optionally Ollama for offline LLM testing

### Key risks / open questions
1. **PromQL quality from local Llama** — mitigated by validation + reflection retries; may need few-shot examples per alarm type in runbooks.
2. **Alarm volume** — POC scope confirmed: low volume. SQLite + single replica is sufficient; skip queue/Postgres.
3. **Which alarm sources besides AlertManager?** (SNMP traps? Netcool?) — currently AlertManager-only by design.
4. **Reflection loop cost** — each reflection is an extra LLM call; acceptable at POC volume.

---

## 5. Suggested first milestone

Phases 0–3 with mocked Prometheus = a demoable core (alarms in → correlated → plan → PromQL investigation → decision) in roughly 3–4 days of work. SSH + report + OpenShift follow.
