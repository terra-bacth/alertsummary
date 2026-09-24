import { useEffect, useState } from 'react';
import { streamUrl, type TsagentEvent } from './api';

export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'offline';

interface StreamInfo {
  state: StreamState;
  lastEventAt: number | null;
}

/**
 * Maintains a reconnecting Server-Sent Events subscription to the tsagent event stream.
 * Events are delivered via the `onEvent` callback so the caller decides how to accumulate.
 */
export function useEventStream(onEvent: (ev: TsagentEvent) => void): StreamInfo {
  const [state, setState] = useState<StreamState>('connecting');
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);

  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    let retry = 0;

    const connect = () => {
      if (closed) return;
      setState((s) => (s === 'live' ? 'reconnecting' : s));
      es = new EventSource(streamUrl());

      es.onopen = () => {
        retry = 0;
        setState('live');
      };

      es.onmessage = (msg) => {
        try {
          const ev = JSON.parse(msg.data) as TsagentEvent;
          setLastEventAt(Date.now());
          onEvent(ev);
        } catch {
          // ignore malformed frames; the stream is best-effort
        }
      };

      es.onerror = () => {
        es?.close();
        setState('reconnecting');
        retry += 1;
        const delay = Math.min(1000 * 2 ** retry, 15000);
        setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      es?.close();
    };
    // onEvent identity is stable in callers (useCallback/memo); do not resubscribe per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { state, lastEventAt };
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
