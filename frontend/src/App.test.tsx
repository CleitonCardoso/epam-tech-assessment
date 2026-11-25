// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import App from './App';
import type { DocumentDetail, DocumentSummary } from './types';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const PARSERS_BODY = {
  ai_mode: 'fake' as const,
  parsers: [
    {
      id: 'pypdf',
      label: 'pypdf',
      output_format: 'text' as const,
      enabled: true,
      requires_api_key: false,
      note: null,
    },
  ],
};

function summaryRow(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
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

function detailFor(id: string, overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    id,
    filename: `${id}.pdf`,
    parser: 'pypdf',
    status: 'completed',
    stage: null,
    attempt: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:05Z',
    page_count: 1,
    warnings: [],
    error: null,
    pages: [],
    summary: null,
    ...overrides,
  };
}

interface ApiHandlers {
  list?: () => Promise<Response>;
  detail?: (id: string) => Promise<Response>;
}

/** Route the three requests App makes on mount to deterministic responses. */
function stubApi(handlers: ApiHandlers = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => {
    const url = String(input);
    if (url === '/api/v1/parsers') {
      return Promise.resolve(jsonResponse(PARSERS_BODY));
    }
    if (url === '/api/v1/documents?limit=20') {
      return handlers.list
        ? handlers.list()
        : Promise.resolve(jsonResponse({ documents: [], next_before: null }));
    }
    const match = /^\/api\/v1\/documents\/([^/?]+)$/.exec(url);
    if (match) {
      const id = decodeURIComponent(match[1]);
      return handlers.detail
        ? handlers.detail(id)
        : Promise.resolve(jsonResponse(detailFor(id)));
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function callsTo(fetchMock: ReturnType<typeof stubApi>, url: string): number {
  return fetchMock.mock.calls.filter(([input]) => String(input) === url).length;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App rehydration after a reload', () => {
  it('rebuilds cards from GET /api/v1/documents and shows the Recent documents heading', async () => {
    const fetchMock = stubApi({
      list: async () =>
        jsonResponse({
          documents: [
            summaryRow({
              id: 'doc-complete',
              filename: 'done.pdf',
              status: 'completed',
              page_count: 2,
            }),
            summaryRow({ id: 'doc-active', filename: 'running.pdf', status: 'parsing' }),
          ],
          next_before: null,
        }),
      detail: async (id) =>
        jsonResponse(
          detailFor(id, {
            filename: id === 'doc-complete' ? 'done.pdf' : 'running.pdf',
            status: id === 'doc-complete' ? 'completed' : 'parsing',
            summary: id === 'doc-complete' ? 'Recovered summary.' : null,
          }),
        ),
    });

    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Recent documents' })).toBeTruthy();
    expect(await screen.findByText('done.pdf')).toBeTruthy();
    expect(screen.getByText('running.pdf')).toBeTruthy();
    // The completed card came back with its stored summary via exactly one detail fetch...
    expect(await screen.findByText('Recovered summary.')).toBeTruthy();
    expect(callsTo(fetchMock, '/api/v1/documents/doc-complete')).toBe(1);
    // ...and is never polled: a second request would mean the terminal stop regressed.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(callsTo(fetchMock, '/api/v1/documents/doc-complete')).toBe(1);
    // The upload form keeps working while all of that happens.
    expect((screen.getByLabelText('PDF files') as HTMLInputElement).disabled).toBe(false);
  });

  it('shows no heading and the normal empty state when the list has no documents', async () => {
    stubApi();
    render(<App />);

    expect(await screen.findByText(/Nothing uploaded yet/)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Recent documents' })).toBeNull();
  });

  it('degrades quietly when the list request fails, keeping the upload form usable', async () => {
    stubApi({
      list: async () =>
        jsonResponse({ detail: 'The job store is unavailable; try again shortly.' }, 503),
    });
    render(<App />);

    expect(await screen.findByText('Could not load recent documents.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Recent documents' })).toBeNull();
    expect((screen.getByLabelText('PDF files') as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByText('Upload failed.')).toBeNull();
  });
});
