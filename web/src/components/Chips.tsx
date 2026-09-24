import type { ReactNode } from 'react';
import { chipFor, type Chip } from '../lib/presentation';

export function Chip({ kind, children }: { kind: Chip; children: ReactNode }) {
  return <span className={`chip chip-${kind}`}>{children}</span>;
}

export function StatusChip({ status }: { status: string }) {
  const kind = chipFor(status);
  const label = status === 'info' ? 'INFO' : status.toUpperCase();
  return <Chip kind={kind}>{label}</Chip>;
}

export function SeverityBadge({ severity }: { severity: string }) {
  const sev = (severity || 'unknown').toLowerCase();
  return <span className={`badge badge-sev-${sev}`}>{sev}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const s = (status || 'open').toLowerCase();
  return <span className={`badge badge-status-${s}`}>{status}</span>;
}
