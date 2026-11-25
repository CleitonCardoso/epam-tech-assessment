import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from 'react';
import {
  ApiError,
  errorMessage,
  fetchDocument,
  fetchParsers,
  rejectedFromErrorBody,
  uploadDocuments,
} from './api';
import { DocumentCard, type DocumentCardState } from './components/DocumentCard';
import { useDocumentPolling } from './hooks/useDocumentPolling';
import { isTerminal, type DocumentDetail, type ParsersResponse, type RejectedFile } from './types';

type ParsersState =
  | { phase: 'loading' }
  | { phase: 'ready'; data: ParsersResponse }
  | { phase: 'error'; message: string };

const FAKE_MODE_BANNER =
  'Fake AI mode — summaries and markdown are deterministic stand-ins, no model was called.';

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function App(): JSX.Element {
  const [parsersState, setParsersState] = useState<ParsersState>({ phase: 'loading' });
  const [parsersReloadKey, setParsersReloadKey] = useState(0);

  const [files, setFiles] = useState<File[]>([]);
  const [selectedParserId, setSelectedParserId] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [rejected, setRejected] = useState<RejectedFile[]>([]);

  const [cards, setCards] = useState<DocumentCardState[]>([]);
  const [activeIds, setActiveIds] = useState<string[]>([]);

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // ── parser list ────────────────────────────────────────────────────────────
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setParsersState({ phase: 'loading' });
    fetchParsers(controller.signal)
      .then((data) => {
        if (!cancelled) setParsersState({ phase: 'ready', data });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setParsersState({
          phase: 'error',
          message: errorMessage(err, 'Could not load the parser list.'),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [parsersReloadKey]);

  const parsers = parsersState.phase === 'ready' ? parsersState.data.parsers : [];
  const enabledParsers = useMemo(() => parsers.filter((parser) => parser.enabled), [parsers]);
  const effectiveParser = useMemo(() => {
    const chosen = parsers.find((parser) => parser.id === selectedParserId && parser.enabled);
    return chosen ?? enabledParsers[0];
  }, [parsers, enabledParsers, selectedParserId]);

  // ── polling updates ────────────────────────────────────────────────────────
  const handleDetail = useCallback((detail: DocumentDetail) => {
    setCards((prev) =>
      prev.map((card) =>
        card.accepted.id === detail.id
          ? {
              ...card,
              detail,
              pollError: null,
              accepted: {
                ...card.accepted,
                filename: detail.filename,
                parser: detail.parser,
                status: detail.status,
              },
            }
          : card,
      ),
    );
    if (isTerminal(detail.status)) {
      setActiveIds((prev) => prev.filter((id) => id !== detail.id));
    }
  }, []);

  const handlePollError = useCallback((id: string, message: string, fatal: boolean) => {
    setCards((prev) =>
      prev.map((card) => (card.accepted.id === id ? { ...card, pollError: message } : card)),
    );
    if (fatal) {
      setActiveIds((prev) => prev.filter((activeId) => activeId !== id));
    }
  }, []);

  useDocumentPolling(activeIds, fetchDocument, {
    onDetail: handleDetail,
    onError: handlePollError,
  });

  // ── form handlers ──────────────────────────────────────────────────────────
  const clearFileInput = (): void => {
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setFiles(Array.from(event.target.files ?? []));
    setUploadError(null);
  };

  const removeFile = (index: number): void => {
    setFiles((prev) => prev.filter((_, fileIndex) => fileIndex !== index));
  };

  const canSubmit =
    parsersState.phase === 'ready' &&
    !uploading &&
    files.length > 0 &&
    effectiveParser !== undefined;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!canSubmit || !effectiveParser) return;

    setUploading(true);
    setUploadError(null);
    setRejected([]);
    try {
      const result = await uploadDocuments(files, effectiveParser.id);
      const newCards: DocumentCardState[] = result.documents.map((accepted) => ({
        accepted,
        detail: null,
        pollError: null,
      }));
      setCards((prev) => [...newCards, ...prev]);
      setActiveIds((prev) => [...result.documents.map((document) => document.id), ...prev]);
      setRejected(result.rejected);
      // Keep the files the server rejected so the batch can be retried after removing the bad
      // ones; the accepted files are done.
      const rejectedNames = new Set(result.rejected.map((file) => file.filename));
      setFiles(files.filter((file) => rejectedNames.has(file.name)));
    } catch (err: unknown) {
      const body = err instanceof ApiError ? err.body : null;
      setRejected(rejectedFromErrorBody(body));
      setUploadError(errorMessage(err, 'Upload failed.'));
    } finally {
      setUploading(false);
      clearFileInput();
    }
  };

  // ── render ─────────────────────────────────────────────────────────────────
  const parserById = new Map(parsers.map((parser) => [parser.id, parser] as const));

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-inner">
          <div>
            <h1>PDF processor</h1>
            <p className="subtitle">
              Upload PDFs and watch the worker parse and summarize them. Status and stage are
              reported by the API; there is no invented progress percentage.
            </p>
          </div>
          {parsersState.phase === 'ready' ? (
            <span className={`mode-chip mode-${parsersState.data.ai_mode}`}>
              AI mode: {parsersState.data.ai_mode}
            </span>
          ) : null}
        </div>
      </header>

      <main className="layout">
        {parsersState.phase === 'ready' && parsersState.data.ai_mode === 'fake' ? (
          <div className="banner banner-fake" role="status">
            {FAKE_MODE_BANNER}
          </div>
        ) : null}

        {parsersState.phase === 'error' ? (
          <div className="banner banner-error" role="alert">
            <div>
              <strong>Could not load the parser list.</strong>
              <p>{parsersState.message}</p>
              <p className="banner-note">
                Uploads stay disabled rather than guessing a parser list.
              </p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={() => setParsersReloadKey((key) => key + 1)}
            >
              Retry
            </button>
          </div>
        ) : null}

        <section className="panel" aria-labelledby="upload-heading">
          <h2 id="upload-heading">Upload</h2>
          <form className="upload-form" onSubmit={handleSubmit}>
            <div className="field field-files">
              <label htmlFor="files">PDF files</label>
              <input
                id="files"
                ref={fileInputRef}
                type="file"
                accept="application/pdf"
                multiple
                onChange={handleFileChange}
                disabled={uploading}
              />
              <p className="help">
                Each file must be a PDF, at most 20 MB. One parser is applied to the entire
                batch.
              </p>
              {files.length > 0 ? (
                <ul className="file-list">
                  {files.map((file, index) => (
                    <li key={`${file.name}-${file.lastModified}-${index}`}>
                      <span className="file-name">{file.name}</span>
                      <span className="file-size">{formatBytes(file.size)}</span>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => removeFile(index)}
                        disabled={uploading}
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            <div className="field">
              <label htmlFor="parser">Parser</label>
              <select
                id="parser"
                value={effectiveParser?.id ?? ''}
                onChange={(event) => setSelectedParserId(event.target.value)}
                disabled={parsersState.phase !== 'ready' || uploading}
              >
                {parsersState.phase === 'loading' ? <option value="">Loading parsers…</option> : null}
                {parsersState.phase === 'error' ? <option value="">Parser list unavailable</option> : null}
                {parsersState.phase === 'ready' && parsers.length === 0 ? (
                  <option value="">No parsers reported</option>
                ) : null}
                {parsersState.phase === 'ready' &&
                parsers.length > 0 &&
                enabledParsers.length === 0 ? (
                  <option value="">No enabled parser</option>
                ) : null}
                {parsers.map((parser) => (
                  <option key={parser.id} value={parser.id} disabled={!parser.enabled}>
                    {parser.label}
                    {parser.enabled ? '' : ` — ${parser.note ?? 'not available'}`}
                  </option>
                ))}
              </select>
              {effectiveParser ? (
                <p className="help">
                  Output format: {effectiveParser.output_format}.{' '}
                  {effectiveParser.requires_api_key
                    ? 'Requires an API key.'
                    : 'No API key required.'}
                  {effectiveParser.note ? ` ${effectiveParser.note}` : ''}
                </p>
              ) : null}
              {parsersState.phase === 'ready' && enabledParsers.length === 0 ? (
                <p className="field-warning" role="status">
                  No parser is currently enabled, so uploads are disabled.
                </p>
              ) : null}
            </div>

            <div className="field field-actions">
              <button type="submit" className="primary" disabled={!canSubmit}>
                {uploading
                  ? 'Uploading…'
                  : files.length > 0
                    ? `Upload ${files.length} ${files.length === 1 ? 'file' : 'files'}`
                    : 'Upload'}
              </button>
            </div>
          </form>
        </section>

        {rejected.length > 0 ? (
          <section className="panel panel-rejected" aria-labelledby="rejected-heading">
            <h2 id="rejected-heading">Rejected files ({rejected.length})</h2>
            <ul className="rejected-list">
              {rejected.map((file, index) => (
                <li key={`${file.filename}-${index}`}>
                  <strong>{file.filename}</strong>
                  <span className="error-code">{file.code}</span>
                  <span>{file.message}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {uploadError ? (
          <div className="banner banner-error" role="alert">
            <div>
              <strong>Upload failed.</strong>
              <p>{uploadError}</p>
            </div>
          </div>
        ) : null}

        <section className="results" aria-labelledby="results-heading">
          <div className="results-head">
            <h2 id="results-heading">Documents ({cards.length})</h2>
            {activeIds.length > 0 ? (
              <span className="polling-note">
                <span className="pulse" aria-hidden="true" />
                Polling {activeIds.length} active {activeIds.length === 1 ? 'document' : 'documents'}{' '}
                every 1.5 s
              </span>
            ) : null}
          </div>
          {cards.length === 0 ? (
            <p className="empty">Nothing uploaded yet. Pick one or more PDFs above.</p>
          ) : (
            <div className="card-grid">
              {cards.map((card) => (
                <DocumentCard
                  key={card.accepted.id}
                  state={card}
                  parser={parserById.get(card.detail?.parser ?? card.accepted.parser)}
                />
              ))}
            </div>
          )}
        </section>
      </main>

      <footer className="app-footer">
        Requests go to the API through this origin&apos;s <code>/api/</code> proxy.
      </footer>
    </div>
  );
}
