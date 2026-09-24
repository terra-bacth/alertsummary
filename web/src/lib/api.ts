// Typed view of the tsagent REST + SSE API and the domain model the dashboard paints.

export type Status = 'info' | 'running' | 'ok' | 'warn' | 'error' | 'retry' | 'refused' | 'unreachable' | 'debug';

export interface TsagentEvent {
  id?: string;
  seq?: number;
  ts?: string;
  type: string;
  status: Status;
  incident_id?: string;
  cluster?: string;
  node?: string;
  agent?: string;
  step?: string;
  title?: string;
  detail?: string;
  data?: Record<string, unknown>;
}

export interface Alarm {
  id: string;
  fingerprint: string;
  status: string;
  name: string;
  severity: string;
  node: string;
  site: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  starts_at?: string | null;
  ends_at?: string | null;
  received_at: string;
  incident_id?: string | null;
  suppressed?: boolean;
  rule_expr?: string;
}

export interface Incident {
  id: string;
  status: 'open' | 'investigating' | 'reported' | 'failed';
  cluster: string;
  group_key: string;
  alarm_ids: string[];
  created_at: string;
  updated_at: string;
  summary: string;
}

export interface PendingGroup {
  group_key: string;
  cluster: string;
  alarms: number;
  firing: number;
  age_seconds: number;
  quiet_seconds: number;
  promotes_at: string;
}

export interface Overview {
  incidents: Incident[];
  alarms: Alarm[];
  counts: {
    incidents: number;
    open_alarms: number;
    alarms: number;
    investigating: number;
    open: number;
    reported: number;
    failed: number;
  };
  by_status: Record<string, number>;
  pending_groups: PendingGroup[];
  in_flight: number;
  collections: Record<string, string>;
}

export interface EvidenceItem {
  id?: string;
  kind: string;
  goal?: string;
  source?: string;
  request?: string;
  result?: string;
  ok?: boolean;
  created_at?: string;
}

export interface IncidentDetail {
  incident: Incident;
  alarms: Alarm[];
  evidence: EvidenceItem[];
}

export interface Report {
  id: string;
  incident_id: string;
  root_cause: string;
  confidence: number;
  recommendations: string[];
  caveats: string[];
  markdown: string;
  data: {
    plan?: { hypotheses?: string[]; checks?: { goal: string; metric_hint?: string }[] };
    promql_evidence?: EvidenceItem[];
    node_evidence?: EvidenceItem[];
    reflections?: { step: string; verdict: string; feedback: string }[];
    decision?: string;
    decision_reason?: string;
  };
  created_at: string;
}

export function apiBase(): string {
  // Allow a remote backend override (a deployed OpenShift Route); fall back to same-origin.
  const env = (import.meta.env.VITE_TSAGENT_API as string | undefined) ?? '';
  return env.replace(/\/$/, '');
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return (await res.json()) as T;
}

export async function postJSON(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

export function streamUrl(): string {
  return `${apiBase()}/api/stream`;
}
