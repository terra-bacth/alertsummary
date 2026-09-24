import type { Overview, PendingGroup } from '../lib/api';
import type { IncidentStateD } from '../hooks/useDashboard';
import { shortId, timeAgo } from '../lib/presentation';
import { SeverityBadge, StatusBadge } from './Chips';

export function Rail({
  overview,
  incidents,
  selectedId,
  onSelect,
}: {
  overview: Overview | null;
  incidents: IncidentStateD[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const counts = overview?.counts;
  return (
    <aside className="rail">
      <div className="rail-head">
        <div className="kpi num-live">
          <div className="kpi-num">{counts?.open_alarms ?? 0}</div>
          <div className="kpi-lab">Firing alarms</div>
        </div>
        <div className="kpi num-ok">
          <div className="kpi-num">{counts?.investigating ?? 0}</div>
          <div className="kpi-lab">Investigating</div>
        </div>
        <div className="kpi num-ok">
          <div className="kpi-num">{counts?.reported ?? 0}</div>
          <div className="kpi-lab">Reports ready</div>
        </div>
      </div>

      <div>
        <div className="list-label">Incidents</div>
        <div className="inc-list" style={{ marginTop: 8 }}>
          {incidents.map((inc) => (
            <div
              key={inc.id}
              className={`inc-card ${inc.id === selectedId ? 'selected' : ''}`}
              onClick={() => onSelect(inc.id)}
            >
              <div className="inc-id">
                {shortId(inc.id)}
                {inc.live && <span className="code"> ●</span>}
              </div>
              <div className="inc-meta">
                <StatusBadge status={inc.incident?.status ?? 'open'} />
                <SeverityBadge severity={inc.worstSev} />
                <span className="code">{inc.incident?.cluster ?? ''}</span>
                <span style={{ marginLeft: 'auto' }}>
                  {timeAgo(inc.incident?.created_at)}
                </span>
              </div>
            </div>
          ))}
          {incidents.length === 0 && (
            <div className="inc-meta" style={{ padding: '2px 4px' }}>
              No incidents yet — inject a demo in the header.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="list-label">Correlation · buffering</div>
        <div className="inc-list" style={{ marginTop: 8 }}>
          {(overview?.pending_groups ?? []).map((g) => (
            <PendingRow key={g.group_key} g={g} />
          ))}
          {(overview?.pending_groups ?? []).length === 0 && (
            <div className="inc-meta" style={{ padding: '2px 4px' }}>
              Quiet — nothing buffered.
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}

function PendingRow({ g }: { g: PendingGroup }) {
  return (
    <div className="inc-card" style={{ cursor: 'default' }}>
      <div className="inc-id code">{g.group_key}</div>
      <div className="inc-meta">
        <span>{g.firing} firing</span>
        <span>·</span>
        <span>{g.age_seconds}s old</span>
        <span style={{ marginLeft: 'auto' }}>{g.cluster}</span>
      </div>
    </div>
  );
}
