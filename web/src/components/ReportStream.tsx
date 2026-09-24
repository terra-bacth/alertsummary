import type { IncidentStateD } from '../hooks/useDashboard';
import type { TsagentEvent } from '../lib/api';
import { AGENT_META, chipFor, clock, severityOf, stageFor } from '../lib/presentation';
import { Chip } from './Chips';

function agentGlyph(agent?: string): string {
  return AGENT_META[agent ?? '']?.icon ?? '•';
}

function eventChip(ev: TsagentEvent) {
  if (!ev.status || ev.status === 'info') {
    return <Chip kind={severityOf(ev.type)}>{ev.type.split('.')[0]}</Chip>;
  }
  return <Chip kind={chipFor(ev.status)}>{ev.status}</Chip>;
}

function dataLine(ev: TsagentEvent): string | null {
  const d = ev.data;
  if (!d || Object.keys(d).length === 0) return null;
  const promql = d.promql;
  const command = d.command;
  const result = d.result;
  if (typeof promql === 'string' && promql) return promql;
  if (typeof command === 'string' && command) {
    const line = `$ ${command}`;
    if (typeof result === 'string' && result) return `${line}\n${result}`;
    return line;
  }
  const rootCause = d.root_cause;
  if (typeof rootCause === 'string' && rootCause) return rootCause;
  return null;
}

export function ReportStream({
  incident,
  now,
}: {
  incident: IncidentStateD;
  now: number;
}) {
  const log = incident.log;
  void now; // time labels refresh via parent re-render
  return (
    <div className="report-stream">
      {log.length === 0 && (
        <div className="stream-empty">
          <span className="big">◌</span>
          Awaiting telemetry for this incident.
          <br />
          Events will stream here as the agents work.
        </div>
      )}
      {log.map((ev, i) => {
        const stage = stageFor(ev.step ?? '');
        const chip = eventChip(ev);
        const data = dataLine(ev);
        return (
          <div key={`${ev.seq ?? ev.id ?? i}-${i}`} className="stream-entry">
            <div className="entry-left">
              <span className="entry-glyph">{agentGlyph(ev.agent)}</span>
              <span className="entry-rail" />
            </div>
            <div className="entry-body">
              <div className="entry-line1">
                {chip}
                <span className="entry-title">{ev.title || ev.type}</span>
                {ev.node && <span className="code">@{ev.node}</span>}
                <span className="entry-meta">
                  {ev.agent || stage.label} · {clock(ev.ts)}
                </span>
              </div>
              {ev.detail && <div className="entry-detail">{ev.detail}</div>}
              {data && <div className="entry-data">{data}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
