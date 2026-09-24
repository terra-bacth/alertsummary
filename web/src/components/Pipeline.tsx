import type { IncidentStateD } from '../hooks/useDashboard';
import { STAGES, stageFor } from '../lib/presentation';

/**
 * The animated pipeline strip. Node states derive from the incident's highest reached stage:
 *   past   → ok (lit green)
 *   current→ active (amber scan)
 *   future → idle (dim)
 * An error anywhere after a stage is past dims that stage red.
 */
export function Pipeline({ incident }: { incident: IncidentStateD }) {
  const reached = incident.stage;
  const status = incident.incident?.status ?? 'open';
  const failed = status === 'failed';
  const reported = status === 'reported';

  const stateFor = (idx: number): 'ok' | 'active' | 'error' | 'idle' => {
    if (reported) return 'ok';
    if (failed && idx <= reached) return 'error';
    if (idx < reached) return 'ok';
    if (idx === reached) return 'active';
    return 'idle';
  };

  const effectiveStages = [
    'ingest',
    'correlate',
    'plan',
    'promql',
    'reflect_promql',
    'decide',
    'node',
    'reflect_node',
    'report',
  ];

  return (
    <div className="pipeline">
      {effectiveStages.map((key, idx) => {
        const meta = stageFor(key);
        const state = stateFor(idx);
        return (
          <div key={key} className="pipe-node">
            <div className={`pipe-chip state-${state}`}>
              <span className="pine-name">
                {meta.icon} {meta.label}
              </span>
              <span className="pine-code">{meta.short}</span>
            </div>
            {idx < effectiveStages.length - 1 && <span className="pipe-link">▸</span>}
          </div>
        );
      })}
    </div>
  );
}

export const STAGE_KEYS = STAGES.map((s) => s.key);
