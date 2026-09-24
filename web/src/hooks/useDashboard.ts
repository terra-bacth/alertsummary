import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getJSON, postJSON, type Incident, type Overview, type Report, type TsagentEvent } from '../lib/api';
import { severityRank, worstSeverity } from '../lib/presentation';
import { useEventStream } from '../lib/useEventStream';

export interface IncidentStateD {
  id: string;
  incident?: Incident;
  alarmNames: string[];
  worstSev: string;
  report?: Report;
  /** highest stage reached so far, derived from event steps */
  stage: number;
  /** compact per-step event log for the timeline */
  log: TsagentEvent[];
  live: boolean;
}

export interface DashboardData {
  overview: Overview | null;
  incidents: IncidentStateD[];
  selectedId: string | null;
  select: (id: string | null) => void;
  selected: IncidentStateD | null;
  streamState: ReturnType<typeof useEventStream>['state'];
  lastEventAt: number | null;
  ignite: () => Promise<void>;
  igniting: boolean;
  now: number;
  totals: { events: number; live: number };
}

const STAGE_ORDER = ['ingest', 'correlate', 'plan', 'promql', 'reflect_promql', 'decide', 'node', 'reflect_node', 'report'];

function stageIndexOf(step: string): number {
  const i = STAGE_ORDER.indexOf(step);
  return i === -1 ? -1 : i;
}

/** Merge a live event into an incident's state. Immutably returns a new map. */
function applyEvent(map: Map<string, IncidentStateD>, ev: TsagentEvent): Map<string, IncidentStateD> {
  const id = ev.incident_id || '';
  if (!id) return map;
  const cur = map.get(id);
  const next: IncidentStateD = cur ?? {
    id,
    alarmNames: [],
    worstSev: 'unknown',
    stage: 0,
    log: [],
    live: false,
  };
  const stageIdx = stageIndexOf(ev.step ?? '');
  const log = [...next.log, ev];
  const updated: IncidentStateD = {
    ...next,
    stage: Math.max(next.stage, stageIdx),
    live: stageIdx >= 0 && ev.status === 'running',
  };
  if (ev.type === 'alarm.received' && ev.data?.alarm) {
    const name = String((ev.data.alarm as { name?: string }).name ?? '');
    const sev = String((ev.data.alarm as { severity?: string }).severity ?? 'unknown');
    if (name && !updated.alarmNames.includes(name)) updated.alarmNames = [...updated.alarmNames, name];
    if (severityRank(sev) < severityRank(updated.worstSev)) updated.worstSev = sev;
  }
  updated.log = log.length > 220 ? log.slice(log.length - 220) : log;
  map.set(id, updated);
  return map;
}

export function useDashboard(): DashboardData {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [reportCache, setReportCache] = useState<Record<string, Report>>({});
  const [states, setStates] = useState<Map<string, IncidentStateD>>(new Map());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [igniting, setIgniting] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const totalsRef = useRef({ events: 0, live: 0 });
  const firstReportWatch = useRef<Set<string>>(new Set());

  const onEvent = useCallback((ev: TsagentEvent) => {
    totalsRef.current.events += 1;
    if (ev.step) totalsRef.current.live += 1;
    setStates((prev) => {
      const next = applyEvent(new Map(prev), ev);
      // Auto-select the first incident to start investigating.
      setSelectedId((cur) => cur ?? (ev.type === 'incident.started' ? ev.incident_id ?? null : null));
      return next;
    });
  }, []);

  const { state: streamState, lastEventAt } = useEventStream(onEvent);

  // Clock tick for countdowns and "ago" labels.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const ov = await getJSON<Overview>('/api/overview');
      setOverview(ov);
      // Seed/refresh state for every incident the API knows about.
      setStates((prev) => {
        const next = new Map(prev);
        for (const inc of ov.incidents) {
          const cur = next.get(inc.id);
          next.set(inc.id, {
            id: inc.id,
            incident: inc,
            alarmNames: cur?.alarmNames ?? [],
            worstSev: cur?.worstSev ?? 'unknown',
            report: cur?.report,
            stage: cur?.stage ?? 0,
            log: cur?.log ?? [],
            live: cur?.live ?? false,
          });
        }
        if (ov.incidents.length > 0) {
          setSelectedId((cur) => cur ?? ov.incidents[0].id);
        }
        return next;
      });
    } catch {
      // backend not up yet; the stream/retry will recover
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  // Pull the report for any incident that just finished (status transitions to reported).
  useEffect(() => {
    const watch = firstReportWatch.current;
    for (const inc of overview?.incidents ?? []) {
      if (inc.status === 'reported' && !watch.has(inc.id)) {
        watch.add(inc.id);
        void getJSON<Report>(`/incidents/${inc.id}/report`).then((r) => {
          setReportCache((c) => ({ ...c, [inc.id]: r }));
        }).catch(() => {});
      }
    }
  }, [overview]);

  const incidents = useMemo(() => {
    return Array.from(states.values())
      .map((s) => {
        let worst = s.worstSev;
        const incident = s.incident;
        if (incident && incident.alarm_ids.length > 0) {
          // severity from overview alarms when available
          const alarms = overview?.alarms ?? [];
          const here = alarms.filter((a) => a.incident_id === incident.id);
          if (here.length) worst = worstSeverity(here.map((a) => a.severity));
        }
        // An incident only exists once correlation has promoted it, so ingest + correlate
        // are by definition complete even when the per-step events predate the incident id.
        const stage = incident ? Math.max(s.stage, 1) : s.stage;
        return {
          ...s,
          stage,
          report: s.report ?? reportCache[s.id],
          worstSev: worst,
          incident,
          live: s.live,
        };
      })
      .sort((a, b) => {
        const at = a.incident?.created_at ?? '';
        const bt = b.incident?.created_at ?? '';
        return bt.localeCompare(at);
      });
  }, [states, reportCache, overview]);

  const selected = useMemo(
    () => incidents.find((i) => i.id === selectedId) ?? incidents[0] ?? null,
    [incidents, selectedId],
  );

  const ignite = useCallback(async () => {
    setIgniting(true);
    try {
      await postJSON('/api/demo?background=true');
    } catch {
      // the demo endpoint may be absent on a minimal deployment; ignore
    } finally {
      setIgniting(false);
    }
  }, []);

  return {
    overview,
    incidents,
    selectedId: selected?.id ?? null,
    select: setSelectedId,
    selected,
    streamState,
    lastEventAt,
    ignite,
    igniting,
    now,
    totals: totalsRef.current,
  };
}
