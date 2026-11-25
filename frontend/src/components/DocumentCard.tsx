import type { DocumentAccepted, DocumentDetail, ParserInfo } from '../types';
import { isTerminal } from '../types';
import { MarkdownView } from './MarkdownView';
import { StatusBadge } from './StatusBadge';

export interface DocumentCardState {
  accepted: DocumentAccepted;
  detail: DocumentDetail | null;
  pollError: string | null;
}

interface DocumentCardProps {
  state: DocumentCardState;
  /** Parser metadata from GET /api/v1/parsers, when that parser is still advertised. */
  parser: ParserInfo | undefined;
}

export function DocumentCard({ state, parser }: DocumentCardProps): JSX.Element {
  const { accepted, detail, pollError } = state;

  const status = detail?.status ?? accepted.status;
  const filename = detail?.filename ?? accepted.filename;
  const parserId = detail?.parser ?? accepted.parser;
  const parserLabel = parser?.label ?? parserId;
  const pages = detail?.pages ?? [];
  const pageCount = detail?.page_count ?? null;
  const warnings = detail?.warnings ?? [];
  const attempt = detail?.attempt ?? 0;
  const stage = detail?.stage ?? null;
  const summary = detail?.summary && detail.summary.trim() !== '' ? detail.summary : null;
  const error = detail?.error ?? null;
  const terminal = isTerminal(status);

  return (
    <article className={`card card-${status}`} aria-label={`Document ${filename}`}>
      <header className="card-header">
        <div className="card-title">
          <h3 title={filename}>{filename}</h3>
          <StatusBadge status={status} />
        </div>
        <dl className="meta">
          <div>
            <dt>Parser</dt>
            <dd>{parserLabel}</dd>
          </div>
          <div>
            <dt>Attempt</dt>
            <dd>{attempt}</dd>
          </div>
          <div>
            <dt>Pages</dt>
            <dd>{pageCount === null ? '—' : pageCount}</dd>
          </div>
          {stage ? (
            <div>
              <dt>Stage</dt>
              <dd>
                <code>{stage}</code>
              </dd>
            </div>
          ) : null}
        </dl>
      </header>

      {!terminal ? (
        <p className="card-hint" role="status">
          {detail ? 'Checking status every 1.5 s.' : 'Queued on the server — waiting for the first status update.'}
        </p>
      ) : null}

      {pollError ? (
        <div className="card-warning" role="status">
          <strong>Status check failed.</strong> {pollError}
          {terminal ? null : ' Retrying in the background with backoff.'}
        </div>
      ) : null}

      {error ? (
        <div className="card-error" role="alert">
          <div className="card-error-head">
            <span className="error-code">{error.code}</span>
            {error.retryable ? <span className="retryable">retryable</span> : null}
          </div>
          <p>{error.message}</p>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <div className="card-warnings">
          <h4>Warnings</h4>
          <ul>
            {warnings.map((warning, index) => (
              <li key={`${index}-${warning}`}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {summary ? (
        <section className="summary" aria-label="Summary">
          <h4>Summary</h4>
          <MarkdownView>{summary}</MarkdownView>
        </section>
      ) : status === 'completed' ? (
        <p className="card-hint">No summary was returned for this document.</p>
      ) : null}

      <details className="pages">
        <summary>Pages ({pages.length})</summary>
        {pages.length === 0 ? (
          <p className="card-hint">No pages stored yet.</p>
        ) : (
          <ol className="page-list">
            {pages.map((page) => (
              <li key={page.page_number} className="page">
                <div className="page-head">
                  <span className="page-number">Page {page.page_number}</span>
                  <span className={`format format-${page.format}`}>{page.format}</span>
                </div>
                {page.format === 'markdown' ? (
                  <MarkdownView>{page.content}</MarkdownView>
                ) : (
                  <pre className="page-text">{page.content}</pre>
                )}
              </li>
            ))}
          </ol>
        )}
      </details>
    </article>
  );
}
