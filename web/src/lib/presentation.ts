// Presentation mapping: every pipe/stage and event type gets an icon, a color chip and a label.
// This is the single place where "cinematic" is encoded per event, keeping components dumb.

export type Chip =
  | 'info' | 'running' | 'ok' | 'warn' | 'error' | 'retry' | 'refused'
  | 'unreachable' | 'debug' | 'pod';

export const STAGES: { key: string; label: string; short: string; icon: string }[] = [
  { key: 'ingest', label: 'Ingest', short: 'RX', icon: '📥' },
  { key: 'correlate', label: 'Correlate', short: 'GRP', icon: '🔗' },
  { key: 'plan', label: 'Plan', short: 'PLN', icon: '🧭' },
  { key: 'promql', label: 'PromQL', short: 'PRM', icon: '📈' },
  { key: 'reflect_promql', label: 'Reflect', short: 'RFL', icon: '🪞' },
  { key: 'decide', label: 'Decide', short: 'DCD', icon: '⚖️' },
  { key: 'node', label: 'Node · SSH', short: 'SSH', icon: '🖥️' },
  { key: 'reflect_node', label: 'Reflect', short: 'RFL', icon: '🪞' },
  { key: 'report', label: 'Report', short: 'RPT', icon: '📝' },
];

const STAGE_BY_KEY = new Map(STAGES.map((s) => [s.key, s]));

export function stageFor(key: string) {
  return STAGE_BY_KEY.get(key) ?? { key, label: key, short: key.slice(0, 3).toUpperCase(), icon: '•' };
}

export const AGENT_META: Record<string, { icon: string; label: string }> = {
  system: { icon: '🛰️', label: 'Ingest' },
  correlator: { icon: '🔗', label: 'Correlator' },
  planner: { icon: '🧭', label: 'Planner' },
  promql: { icon: '📈', label: 'PromQL Agent' },
  reflection: { icon: '🪞', label: 'Reflector' },
  decision: { icon: '⚖️', label: 'Decision' },
  node_agent: { icon: '🖥️', label: 'Node Agent' },
  report: { icon: '📝', label: 'Report Agent' },
  orchestrator: { icon: '🎻', label: 'Orchestrator' },
};

const STATUS: Record<string, Chip> = {
  info: 'info',
  running: 'running',
  ok: 'ok',
  warn: 'warn',
  error: 'error',
  retry: 'retry',
  refused: 'refused',
  unreachable: 'unreachable',
  debug: 'debug',
};

export function chipFor(status: string): Chip {
  return STATUS[status] ?? 'info';
}

// A status chip fallback for events that carry a type without an explicit status (defensive).
export function severityOf(type: string): Chip {
  if (/refused|redirected|repeat/i.test(type)) return 'refused';
  if (/retry/i.test(type)) return 'retry';
  if (/executed|approved|completed|ready|opened|resolved|received/i.test(type)) return 'ok';
  if (/rejected|offline|fallback|exhausted/i.test(type)) return 'warn';
  if (/failed|unreachable|error/i.test(type)) return 'error';
  return 'info';
}

export function timeAgo(iso?: string): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 2) return 'just now';
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function clock(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour12: false });
}

export function shortId(id?: string): string {
  if (!id) return '—';
  return id.length > 10 ? `${id.slice(0, 6)}…${id.slice(-3)}` : id;
}

export function confidenceLabel(c: number): string {
  return `${Math.round((c ?? 0) * 100)}%`;
}

export function severityRank(sev: string): number {
  const order = ['critical', 'major', 'error', 'warning', 'minor', 'info', 'unknown'];
  const i = order.indexOf((sev || 'unknown').toLowerCase());
  return i === -1 ? order.length : i;
}

export function worstSeverity(sev: string[]): string {
  return [...sev].sort((a, b) => severityRank(a) - severityRank(b))[0] ?? 'unknown';
}
