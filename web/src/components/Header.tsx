import type { StreamState } from '../lib/useEventStream';

export function Header({
  streamState,
  igniting,
  onIgnite,
}: {
  streamState: StreamState;
  igniting: boolean;
  onIgnite: () => void;
}) {
  const label =
    streamState === 'live'
      ? 'LIVE'
      : streamState === 'connecting'
        ? 'CONNECTING'
        : streamState === 'reconnecting'
          ? 'RECONNECTING'
          : 'OFFLINE';
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark">🛰️</div>
        <div>
          <div className="brand-title">
            TS<span className="accent">AGENT</span>
          </div>
          <div className="brand-sub">Multi-agent alarm triage · Mission Control</div>
        </div>
      </div>
      <button className="ignite" onClick={onIgnite} disabled={igniting}>
        {igniting ? '◆ INJECTING…' : '▶ IGNITE DEMO'}
      </button>
      <div className="conn">
        <span className={`conn-dot ${streamState}`} />
        {label}
      </div>
    </header>
  );
}
