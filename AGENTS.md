# Agent Naming Convention — One Piece

Each agent in the tsagent pipeline is named after a One Piece character whose role mirrors their function.

## Mapping

| Agent | One Piece Name | Role | File |
|-------|---------------|------|------|
| Planner | **Nami** | Navigator — charts the investigation course, decides which metrics to check | `src/tsagent/graph/nodes/planner.py` |
| PromQL | **Usopp** | Sniper/Spotter — writes targeted queries, observes from afar | `src/tsagent/graph/nodes/promql.py` |
| Reflection | **Tony Tony Chopper** | Doctor — diagnoses whether output is correct and sufficient | `src/tsagent/graph/nodes/reflect.py` |
| Decision | **Roronoa Zoro** | Swordsman — cuts through to the decision: metrics or node access? | `src/tsagent/graph/nodes/decide.py` |
| Node Agent | **Sanji** | Cook/Field agent — goes hands-on via SSH, gets into the kitchen | `src/tsagent/graph/nodes/node_ssh.py` |
| Report | **Nico Robin** | Archaeologist — pieces together the evidence into the RCA | `src/tsagent/graph/nodes/report.py` |

## Why One Piece?

- **Nami** navigates: she charts the course for the crew, just as the planner charts the investigation.
- **Usopp** spots: he observes from a distance and calls out targets, just as the PromQL agent spots metrics.
- **Chopper** diagnoses: he is the doctor who reviews health, just as the reflector diagnoses output quality.
- **Zoro** decides: he is decisive and cuts through confusion, just as the decision node cuts to the verdict.
- **Sanji** cooks: he goes into the kitchen and gets hands-on, just as the node agent goes onto the nodes.
- **Robin** pieces together: she is the archaeologist who assembles the puzzle, just as the report agent assembles the RCA.

## Token Optimization Changes

The following changes were made to fit within a 4096-token context on CPU inference:

### Config Defaults (`config.py`)
| Setting | Old | New |
|---------|-----|-----|
| `max_promql_queries` | 6 | 3 |
| `max_node_commands` | 8 | 4 |
| `max_retries` | 2 | 1 |
| `max_tokens` (report) | 900 | 400 |
| `evidence_items` | 6 | 3 |
| `evidence_max_chars` | 1500 | 600 |
| `evidence_result_chars` | 250 | 120 |
| `output_max_chars` (SSH) | 1500 | 600 |

### Schema Constraints (`schemas.py`)
- `RCA.root_cause`: max 200 chars
- `RCA.recommendations`: max 3 items
- `RCA.caveats`: max 2 items
- `RCA.timeline`: max 3 items
- `Reflection.feedback`: max 150 chars
- `Decision.reason`: max 150 chars
- `NodeCommand.rationale`: max 100 chars
- `InvestigationPlan.hypotheses`: max 2
- `InvestigationPlan.checks`: max 3

### Context Truncation (`context.py`)
- `alarm_digest`: limit 12 → 5
- `evidence_digest`: limit 12→5, max_chars 4000→800, result_chars 400→150

### Agent Prompts
- All system prompts compacted with One Piece character names
- Node agent loop: feeds back only last 2 commands instead of full history
- Report prompt: removed verbose evidence interpretation section
