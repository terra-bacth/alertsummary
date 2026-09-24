import { useMemo, useState } from 'react';
import type { IncidentStateD } from '../hooks/useDashboard';
import { confidenceLabel, shortId } from '../lib/presentation';
import { Pipeline } from './Pipeline';
import { ReportPanel } from './ReportPanel';
import { ReportStream } from './ReportStream';
import { SeverityBadge, StatusBadge } from './Chips';

export function Stage({
  incident,
  now,
}: {
  incident: IncidentStateD | null;
  now: number;
}) {
  const [filter, setFilter] = useState<string | null>(null);
  const log = useMemo(() => {
    if (!incident) return [];
    if (!filter) return incident.log;
    return incident.log.filter((e) => e.step === filter);
  }, [incident, filter]);

  if (!incident) {
    return (
      <div className="stage">
        <div className="stage-empty">
          <div className="hero">
            <div className="ring">🛰️</div>
            <h2>No active incident selected</h2>
            <p>
              This is the mission-control view for <strong>tsagent</strong> — the multi-agent
              system that turns AlertManager alarms into root-cause reports. Watch alarms land,
              correlation group them, and the agent pipeline plan → query → reflect → decide →
              node → report in real time.
            </p>
            <div className="tips">
              <div><span className="k">▶ IGNITE DEMO</span> — replay a full scripted investigation</div>
              <div><span className="k">POST /alerts</span> — point AlertManager at this API</div>
              <div><span className="k">/api/stream</span> — the live event feed this page reads</div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const status = incident.incident?.status ?? 'open';
  const filtered: IncidentStateD = { ...incident, log };

  return (
    <div className="stage">
      <div className="focusbar">
        <span className="focus-name">{shortId(incident.id)}</span>
        <StatusBadge status={status} />
        <SeverityBadge severity={incident.worstSev} />
        <span className="focus-summary">
          {incident.incident?.summary || `${incident.alarmNames.length} alarm(s)`}
        </span>
        {incident.report && (
          <div className="focus-conf">
            <span className="conf-num">{confidenceLabel(incident.report.confidence)}</span>
            <span className="conf-lab">CONF</span>
          </div>
        )}
      </div>

      <Pipeline incident={incident} />

      <div className="stage-body">
        <div className="panel">
          <div className="panel-head">
            <span>Investigation timeline</span>
            <span className="spacer" />
            <FilterPills active={filter} onChange={(f) => setFilter(f)} />
          </div>
          <ReportStream incident={filtered} now={now} />
          <div className="panel-foot">
            <span>{incident.log.length} events</span>
            <span className="spacer" style={{ flex: 1 }} />
            <span>live feed · /api/stream</span>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <span>RCA report</span>
            <span className="spacer" />
            <span className="code">{incident.incident?.cluster || ''}</span>
          </div>
          <ReportPanel report={incident.report} />
        </div>
      </div>
    </div>
  );
}

const FILTERS = [
  { key: null, label: 'All' },
  { key: 'ingest', label: 'Ingest' },
  { key: 'correlate', label: 'Correlate' },
  { key: 'plan', label: 'Plan' },
  { key: 'promql', label: 'PromQL' },
  { key: 'reflect_promql', label: 'Reflect' },
  { key: 'decide', label: 'Decide' },
  { key: 'node', label: 'Node' },
  { key: 'reflect_node', label: 'Reflect·N' },
  { key: 'report', label: 'Report' },
];

function FilterPills({ active, onChange }: { active: string | null; onChange: (f: string | null) => void }) {
  return (
    <div className="pend-row" style={{ border: 'none', background: 'transparent', gap: 4, padding: 0 }}>
      {FILTERS.map((f) => (
        <button
          key={f.key ?? 'all'}
          onClick={() => onChange(f.key)}
          className="chip"
          style={{
            cursor: 'pointer',
            borderColor: active === f.key ? 'var(--amber)' : 'var(--line)',
            color: active === f.key ? 'var(--amber)' : 'var(--ink-dim)',
            background: active === f.key ? 'var(--amber-soft)' : 'transparent',
          }}
        >
          {f.label}
        </button>
      ))}
    </div>
  );
}
