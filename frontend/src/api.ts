import type {
  DocumentDetail,
  ParsersResponse,
  RejectedFile,
  UploadResponse,
} from './types';

/**
 * Every request is relative and same-origin: in production nginx proxies `/api/` to the API
 * service, in development the Vite proxy does the same. There is intentionally no base URL
 * setting, so no backend hostname can leak into the bundle.
 */
const API_BASE = '/api/v1';

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * Pull a human-readable message out of an arbitrary FastAPI error body.
 * Handles: `{detail: "..."}`, `{detail: {message}}`, `{detail: [{loc, msg}]}` (422 validation),
 * plain strings and unknown shapes.
 */
export function describeErrorBody(body: unknown): string | null {
  if (body === null || body === undefined) return null;
  if (typeof body === 'string') {
    const trimmed = body.trim();
    return trimmed || null;
  }
  if (!isRecord(body)) return null;

  const detail = body.detail ?? body.message ?? body.error;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (!isRecord(item)) return null;
        const msg = item.msg ?? item.message;
        if (typeof msg !== 'string') return null;
        const loc = Array.isArray(item.loc) ? item.loc.join('.') : null;
        return loc ? `${loc}: ${msg}` : msg;
      })
      .filter((part): part is string => part !== null);
    if (parts.length > 0) return parts.join('; ');
  }
  if (isRecord(detail)) {
    const nested = describeErrorBody(detail);
    if (nested) return nested;
  }
  return null;
}

function isRejectedFile(value: unknown): value is RejectedFile {
  return (
    isRecord(value) &&
    typeof value.filename === 'string' &&
    typeof value.code === 'string' &&
    typeof value.message === 'string'
  );
}

function rejectedFromArray(value: unknown): RejectedFile[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter(isRejectedFile);
}

/**
 * Some error responses (notably 413/422 for an all-invalid batch) may still carry the
 * per-file `rejected` list. Surface it instead of collapsing everything into one message.
 * Accepts both `{rejected: [...]}` and `{detail: {rejected: [...]}}`.
 */
export function rejectedFromErrorBody(body: unknown): RejectedFile[] {
  if (isRecord(body)) {
    const direct = rejectedFromArray(body.rejected);
    if (direct) return direct;
    if (isRecord(body.detail)) {
      const nested = rejectedFromArray(body.detail.rejected);
      if (nested) return nested;
    }
  }
  return [];
}

export function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return err.status > 0 ? `${err.message} (HTTP ${err.status})` : err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export async function fetchParsers(signal?: AbortSignal): Promise<ParsersResponse> {
  const response = await fetch(`${API_BASE}/parsers`, {
    headers: { Accept: 'application/json' },
    signal,
  });
  const body = await readBody(response);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      describeErrorBody(body) ?? `Could not load parsers (HTTP ${response.status})`,
      body,
    );
  }
  if (!isRecord(body) || !Array.isArray(body.parsers)) {
    throw new ApiError(response.status, 'Malformed parser response from the API.', body);
  }
  return body as unknown as ParsersResponse;
}

export async function uploadDocuments(files: File[], parser: string): Promise<UploadResponse> {
  const form = new FormData();
  // Repeated `files` field: one entry per file, in the same order as selected.
  for (const file of files) {
    form.append('files', file, file.name);
  }
  form.append('parser', parser);
  // Do NOT set Content-Type: fetch must add the multipart boundary itself.

  const response = await fetch(`${API_BASE}/documents`, {
    method: 'POST',
    body: form,
    headers: { Accept: 'application/json' },
  });
  const body = await readBody(response);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      describeErrorBody(body) ?? `Upload failed (HTTP ${response.status})`,
      body,
    );
  }
  if (!isRecord(body)) {
    throw new ApiError(response.status, 'Malformed upload response from the API.', body);
  }
  return {
    documents: Array.isArray(body.documents)
      ? (body.documents as UploadResponse['documents'])
      : [],
    rejected: rejectedFromArray(body.rejected) ?? [],
  };
}

export async function fetchDocument(id: string, signal?: AbortSignal): Promise<DocumentDetail> {
  const response = await fetch(`${API_BASE}/documents/${encodeURIComponent(id)}`, {
    headers: { Accept: 'application/json' },
    signal,
  });
  const body = await readBody(response);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      describeErrorBody(body) ?? `Could not load document (HTTP ${response.status})`,
      body,
    );
  }
  if (!isRecord(body)) {
    throw new ApiError(response.status, 'Malformed document response from the API.', body);
  }
  return body as unknown as DocumentDetail;
}
