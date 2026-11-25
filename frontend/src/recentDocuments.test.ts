import { describe, expect, it } from 'vitest';
import {
  mergeRecentCards,
  pollingIds,
  summariesToCards,
  summaryToCard,
  terminalIds,
} from './recentDocuments';
import type { DocumentCardState } from './components/DocumentCard';
import type { DocumentStatus, DocumentSummary } from './types';

function summary(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: 'doc-1',
    filename: 'a.pdf',
    parser: 'pypdf',
    status: 'queued',
    page_count: null,
    created_at: '2026-01-01T00:00:00Z',
    status_url: '/api/v1/documents/doc-1',
    ...overrides,
  };
}

function cardFor(id: string, status: DocumentStatus): DocumentCardState {
  return summaryToCard(summary({ id, status, status_url: `/api/v1/documents/${id}` }));
}

describe('recent documents mapping', () => {
  it('maps a document summary onto the existing card state', () => {
    const card = summaryToCard(
      summary({
        id: 'abc',
        filename: 'report.pdf',
        parser: 'gemini',
        status: 'completed',
        page_count: 3,
        status_url: '/api/v1/documents/abc',
      }),
    );

    expect(card).toEqual({
      source: 'recent',
      accepted: {
        id: 'abc',
        filename: 'report.pdf',
        parser: 'gemini',
        status: 'completed',
        status_url: '/api/v1/documents/abc',
      },
      detail: null,
      pollError: null,
    });
    // page_count/created_at are summary-only: no fake DocumentDetail is invented from them.
    expect(card.detail).toBeNull();
  });

  it('maps a list response in API order', () => {
    const cards = summariesToCards({
      documents: [
        summary({ id: 'first', filename: 'newest.pdf' }),
        summary({ id: 'second', filename: 'older.pdf', status: 'parsing' }),
      ],
      next_before: 1700000000.25,
    });

    expect(cards.map((card) => card.accepted.id)).toEqual(['first', 'second']);
    expect(cards.map((card) => card.accepted.status)).toEqual(['queued', 'parsing']);
    expect(cards.every((card) => card.source === 'recent')).toBe(true);
  });

  it('returns an empty array for an empty documents list', () => {
    expect(summariesToCards({ documents: [], next_before: null })).toEqual([]);
    expect(summariesToCards([])).toEqual([]);
  });

  it('returns an empty array when the documents key is missing', () => {
    expect(summariesToCards({ next_before: null })).toEqual([]);
    expect(summariesToCards({})).toEqual([]);
    expect(summariesToCards(null)).toEqual([]);
    expect(summariesToCards(undefined)).toEqual([]);
    expect(summariesToCards('not a list')).toEqual([]);
  });

  it('keeps valid rows and skips malformed ones instead of blanking the whole list', () => {
    const cards = summariesToCards({
      documents: [
        summary({ id: 'good' }),
        { id: 'no-status', filename: 'x.pdf', parser: 'pypdf', created_at: 'x', status_url: '/x' },
        { ...summary({ id: 'bad-status' }), status: 'done' },
        summary({ id: 'also-good', status: 'failed' }),
      ],
    });

    expect(cards.map((card) => card.accepted.id)).toEqual(['good', 'also-good']);
  });

  it('polls only the non-terminal rehydrated cards', () => {
    const cards = [
      cardFor('queued', 'queued'),
      cardFor('parsing', 'parsing'),
      cardFor('summarizing', 'summarizing'),
      cardFor('retrying', 'retrying'),
      cardFor('completed', 'completed'),
      cardFor('failed', 'failed'),
    ];

    expect(pollingIds(cards)).toEqual(['queued', 'parsing', 'summarizing', 'retrying']);
    expect(terminalIds(cards)).toEqual(['completed', 'failed']);
  });

  it('merges rehydrated cards once, keeping the existing array when nothing is new', () => {
    const existing = [cardFor('doc-1', 'parsing')];
    const incoming = [cardFor('doc-1', 'parsing'), cardFor('doc-2', 'completed')];

    const merged = mergeRecentCards(existing, incoming);
    expect(merged.map((card) => card.accepted.id)).toEqual(['doc-1', 'doc-2']);
    expect(merged).toHaveLength(2);

    const unchanged = mergeRecentCards(merged, [cardFor('doc-1', 'parsing')]);
    expect(unchanged).toBe(merged);
  });
});
