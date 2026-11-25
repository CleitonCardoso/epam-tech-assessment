import type { DocumentStatus } from './types';

/**
 * Badge text for each wire status. The badge is uppercased by CSS (`.badge` sets
 * `text-transform: uppercase`), so this is only a title-cased rendering of the exact status the
 * API reports - it never invents a status.
 */
const STATUS_LABELS: Record<DocumentStatus, string> = {
  queued: 'Queued',
  parsing: 'Parsing',
  summarizing: 'Summarizing',
  retrying: 'Retrying',
  completed: 'Completed',
  failed: 'Failed',
};

export function statusLabel(status: DocumentStatus): string {
  return STATUS_LABELS[status];
}

/**
 * Exactly the class names declared in styles.css: `.badge` plus `.status-<wire status>`.
 * Extracted so the mapping can be asserted for every status without rendering React.
 */
export function statusBadgeClass(status: DocumentStatus): string {
  return `badge status-${status}`;
}
