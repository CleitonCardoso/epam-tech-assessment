import type { DocumentCardState } from './components/DocumentCard';
import {
  isDocumentStatus,
  isTerminal,
  type DocumentSummary,
} from './types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isDocumentSummary(value: unknown): value is DocumentSummary {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === 'string' &&
    value.id !== '' &&
    typeof value.filename === 'string' &&
    typeof value.parser === 'string' &&
    isDocumentStatus(value.status) &&
    (typeof value.page_count === 'number' || value.page_count === null || value.page_count === undefined) &&
    typeof value.created_at === 'string' &&
    typeof value.status_url === 'string'
  );
}

/**
 * Map one summary row from GET /api/v1/documents onto the existing card state. `detail` stays
 * null: the card renders from the summary until a poll (non-terminal) or the one-shot detail
 * hydration (terminal) fills it in. `page_count`/`created_at` are summary-only fields and are
 * intentionally not invented into a fake `DocumentDetail`.
 */
export function summaryToCard(summary: DocumentSummary): DocumentCardState {
  return {
    source: 'recent',
    accepted: {
      id: summary.id,
      filename: summary.filename,
      parser: summary.parser,
      status: summary.status,
      status_url: summary.status_url,
    },
    detail: null,
    pollError: null,
  };
}

/**
 * Map a list response (or a bare array, for convenience) onto card state. Tolerant by design:
 * a missing/empty `documents` key, a non-array value, or an individual malformed row must not
 * throw or blank the upload form - the caller simply gets the valid cards it can show.
 */
export function summariesToCards(payload: unknown): DocumentCardState[] {
  const rows = Array.isArray(payload)
    ? payload
    : isRecord(payload) && Array.isArray(payload.documents)
      ? payload.documents
      : [];
  return rows.filter(isDocumentSummary).map(summaryToCard);
}

/** Ids of rehydrated cards whose status is not terminal: the only ones that keep polling. */
export function pollingIds(cards: DocumentCardState[]): string[] {
  return cards
    .filter((card) => !isTerminal(card.accepted.status))
    .map((card) => card.accepted.id);
}

/**
 * Ids of rehydrated cards that are already final. They are never polled; the caller fetches
 * their detail once so a completed summary/pages survive a reload without an ongoing loop.
 */
export function terminalIds(cards: DocumentCardState[]): string[] {
  return cards
    .filter((card) => isTerminal(card.accepted.status))
    .map((card) => card.accepted.id);
}

/**
 * Append rehydrated cards that are not already present (the list and an in-flight upload can
 * overlap). Keeps the existing array identity when there is nothing new, to avoid a re-render.
 */
export function mergeRecentCards(
  existing: DocumentCardState[],
  incoming: DocumentCardState[],
): DocumentCardState[] {
  const seen = new Set(existing.map((card) => card.accepted.id));
  const fresh: DocumentCardState[] = [];
  for (const card of incoming) {
    const id = card.accepted.id;
    if (seen.has(id)) continue;
    seen.add(id);
    fresh.push(card);
  }
  return fresh.length === 0 ? existing : [...existing, ...fresh];
}
