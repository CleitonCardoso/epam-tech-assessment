/**
 * Wire types — these mirror docs/CONTRACT.md §5 and app/models.py exactly. Do not add fields
 * that the API does not send and do not rename any: the names below are the contract.
 */

export type AiMode = 'live' | 'fake';

/**
 * The six statuses the API can report, in the order a document moves through them
 * (terminal ones last). Kept as a const array so the wire guard and the tests can iterate
 * over exactly the values of `DocumentStatus`.
 */
export const DOCUMENT_STATUSES = [
  'queued',
  'parsing',
  'summarizing',
  'retrying',
  'completed',
  'failed',
] as const;

export type DocumentStatus = (typeof DOCUMENT_STATUSES)[number];

export function isDocumentStatus(value: unknown): value is DocumentStatus {
  return (
    typeof value === 'string' && (DOCUMENT_STATUSES as readonly string[]).includes(value)
  );
}

export type PageFormat = 'text' | 'markdown';

export type Stage = 'parsing' | 'summarizing';

/** GET /api/v1/parsers → parsers[] */
export interface ParserInfo {
  id: string;
  label: string;
  output_format: PageFormat;
  enabled: boolean;
  requires_api_key: boolean;
  note: string | null;
}

/** GET /api/v1/parsers */
export interface ParsersResponse {
  ai_mode: AiMode;
  parsers: ParserInfo[];
}

/** POST /api/v1/documents → documents[] (202) */
export interface DocumentAccepted {
  id: string;
  filename: string;
  parser: string;
  status: DocumentStatus;
  status_url: string;
}

/** POST /api/v1/documents → rejected[] (202, or an error body) */
export interface RejectedFile {
  filename: string;
  code: string;
  message: string;
}

/** POST /api/v1/documents → 202 */
export interface UploadResponse {
  documents: DocumentAccepted[];
  rejected: RejectedFile[];
}

/** GET /api/v1/documents -> documents[]; one summary row per known (non-expired) document. */
export interface DocumentSummary {
  id: string;
  filename: string;
  parser: string;
  status: DocumentStatus;
  page_count: number | null;
  created_at: string;
  status_url: string;
}

/** GET /api/v1/documents -> 200 (newest first; `next_before` is the next page cursor). */
export interface DocumentListResponse {
  documents: DocumentSummary[];
  next_before: number | null;
}

/** ErrorInfo from app/models.py, surfaced on a failed document. */
export interface ErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
}

/** One extracted page; `page_number` is 1-based and the API returns pages in document order. */
export interface PageResult {
  page_number: number;
  format: PageFormat;
  content: string;
}

/** GET /api/v1/documents/{id} → DocumentDetail (DocumentJob fields + pages + summary). */
export interface DocumentDetail {
  id: string;
  filename: string;
  parser: string;
  status: DocumentStatus;
  stage: Stage | null;
  attempt: number;
  created_at: string;
  updated_at: string;
  page_count: number | null;
  warnings: string[];
  error: ErrorInfo | null;
  pages: PageResult[];
  summary: string | null;
}

export function isTerminal(status: DocumentStatus): boolean {
  return status === 'completed' || status === 'failed';
}
