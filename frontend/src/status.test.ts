import { describe, expect, it } from 'vitest';
import { statusBadgeClass, statusLabel } from './status';
import { DOCUMENT_STATUSES, isDocumentStatus, isTerminal } from './types';

describe('document status presentation', () => {
  it('has a human label for every wire status', () => {
    const labels = Object.fromEntries(
      DOCUMENT_STATUSES.map((status) => [status, statusLabel(status)]),
    );
    expect(labels).toEqual({
      queued: 'Queued',
      parsing: 'Parsing',
      summarizing: 'Summarizing',
      retrying: 'Retrying',
      completed: 'Completed',
      failed: 'Failed',
    });
  });

  it('labels are distinct and only title-case the exact API status', () => {
    const labels = DOCUMENT_STATUSES.map((status) => statusLabel(status));
    expect(new Set(labels).size).toBe(DOCUMENT_STATUSES.length);
    labels.forEach((label, index) => {
      expect(label.toLowerCase()).toBe(DOCUMENT_STATUSES[index]);
    });
  });

  it('maps every status onto the badge + status-<status> classes used by styles.css', () => {
    const classes = Object.fromEntries(
      DOCUMENT_STATUSES.map((status) => [status, statusBadgeClass(status)]),
    );
    expect(classes).toEqual({
      queued: 'badge status-queued',
      parsing: 'badge status-parsing',
      summarizing: 'badge status-summarizing',
      retrying: 'badge status-retrying',
      completed: 'badge status-completed',
      failed: 'badge status-failed',
    });
  });

  it('treats completed and failed as the only terminal statuses', () => {
    const terminal = DOCUMENT_STATUSES.filter((status) => isTerminal(status));
    expect(terminal).toEqual(['completed', 'failed']);
  });

  it('accepts only the six statuses from the wire contract', () => {
    for (const status of DOCUMENT_STATUSES) {
      expect(isDocumentStatus(status)).toBe(true);
    }
    expect(isDocumentStatus('done')).toBe(false);
    expect(isDocumentStatus('')).toBe(false);
    expect(isDocumentStatus(undefined)).toBe(false);
    expect(isDocumentStatus(null)).toBe(false);
    expect(isDocumentStatus(3)).toBe(false);
  });
});
