import type { DocumentStatus } from '../types';

/** Renders the exact status string the API reports — never an invented one. */
export function StatusBadge({ status }: { status: DocumentStatus }): JSX.Element {
  return <span className={`badge status-${status}`}>{status}</span>;
}
