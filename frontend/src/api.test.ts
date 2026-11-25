import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  describeErrorBody,
  errorMessage,
  fetchDocument,
  fetchRecentDocuments,
  rejectedFromErrorBody,
  uploadDocuments,
} from './api';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function textResponse(body: string, status: number): Response {
  return {
    ok: false,
    status,
    text: async () => body,
  } as unknown as Response;
}

type FetchMock = ReturnType<typeof vi.fn>;

/** Replace the global transport and return the mock so URL/init assertions are possible. */
function stubFetch(implementation: () => Promise<Response>): FetchMock {
  const mock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => implementation());
  vi.stubGlobal('fetch', mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api client error handling', () => {
  it('surfaces a 4xx {"detail": "..."} body as an ApiError with the server message', async () => {
    stubFetch(async () =>
      jsonResponse({ detail: 'The job store is unavailable; try again shortly.' }, 503),
    );

    const error: unknown = await fetchRecentDocuments().catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(503);
    expect((error as ApiError).message).toBe(
      'The job store is unavailable; try again shortly.',
    );
    expect(errorMessage(error, 'fallback')).toBe(
      'The job store is unavailable; try again shortly. (HTTP 503)',
    );
  });

  it('flattens a FastAPI 422 detail array into located field errors', async () => {
    stubFetch(async () =>
      jsonResponse(
        {
          detail: [
            {
              type: 'enum',
              loc: ['body', 'parser'],
              msg: 'Input should be a valid parser id',
              input: 'nope',
            },
            { type: 'missing', loc: ['body', 'files'], msg: 'Field required' },
          ],
        },
        422,
      ),
    );

    const error: unknown = await uploadDocuments([], 'nope').catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).message).toBe(
      'body.parser: Input should be a valid parser id; body.files: Field required',
    );
    expect(describeErrorBody((error as ApiError).body)).toContain('body.parser');
  });

  it('rethrows a transport failure unchanged, without inventing an HTTP status', async () => {
    stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));

    const error: unknown = await fetchDocument('unknown-id').catch((err: unknown) => err);

    expect(error).toBeInstanceOf(TypeError);
    expect(error).not.toBeInstanceOf(ApiError);
    expect(errorMessage(error, 'fallback')).toBe('Failed to fetch');
  });

  it('falls back to a status-based message when the error body is empty', async () => {
    stubFetch(async () => textResponse('', 502));

    const error: unknown = await fetchRecentDocuments().catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(502);
    expect((error as ApiError).message).toBe(
      'Could not load recent documents (HTTP 502)',
    );
    expect((error as ApiError).body).toBeNull();
  });

  it('keeps a non-JSON error body verbatim instead of dropping it', async () => {
    stubFetch(async () => textResponse('<html>bad gateway</html>', 502));

    const error: unknown = await fetchRecentDocuments().catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).body).toBe('<html>bad gateway</html>');
    expect((error as ApiError).message).toBe('<html>bad gateway</html>');
  });

  it('keeps per-file rejections out of an error body available to the form', () => {
    expect(
      rejectedFromErrorBody({
        detail: {
          rejected: [{ filename: 'broken.pdf', code: 'invalid_pdf', message: 'not a PDF' }],
        },
      }),
    ).toEqual([{ filename: 'broken.pdf', code: 'invalid_pdf', message: 'not a PDF' }]);
    expect(rejectedFromErrorBody({ detail: 'plain text' })).toEqual([]);
  });
});

describe('api client list endpoint', () => {
  it('calls GET /api/v1/documents?limit=20 on the first page and parses the envelope', async () => {
    const summary = {
      id: 'doc-1',
      filename: 'a.pdf',
      parser: 'pypdf',
      status: 'queued',
      page_count: null,
      created_at: '2026-01-01T00:00:00Z',
      status_url: '/api/v1/documents/doc-1',
    };
    const fetchMock = stubFetch(async () =>
      jsonResponse({ documents: [summary], next_before: 1700000000.5 }),
    );

    const list = await fetchRecentDocuments();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v1/documents?limit=20');
    expect(init).toMatchObject({ headers: { Accept: 'application/json' } });
    expect(list.documents).toEqual([summary]);
    expect(list.next_before).toBe(1700000000.5);
  });

  it('passes the previous next_before as the before cursor on later pages', async () => {
    const fetchMock = stubFetch(async () => jsonResponse({ documents: [], next_before: null }));

    await fetchRecentDocuments(20, 1700000000.5);

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/documents?limit=20&before=1700000000.5');
  });
});
