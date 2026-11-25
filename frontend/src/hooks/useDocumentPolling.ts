import { useEffect, useRef } from 'react';
import { ApiError } from '../api';
import { isTerminal, type DocumentDetail } from '../types';

/** Per the delivery requirements: poll non-terminal documents every 1.5 s. */
export const POLL_INTERVAL_MS = 1500;

/** On HTTP errors back off 3 s → 6 s → 12 s → … capped here. */
const MAX_BACKOFF_MS = 30_000;

interface PollingCallbacks {
  onDetail: (detail: DocumentDetail) => void;
  /** `fatal` means the document is gone (404/410) and polling has been stopped. */
  onError: (id: string, message: string, fatal: boolean) => void;
}

/**
 * One recursive `setTimeout` chain per document id. Each chain is cancelled and aborted when
 * the hook unmounts or when the id set changes, so no timer or fetch outlives the component
 * and terminal documents are never polled again.
 */
export function useDocumentPolling(
  ids: string[],
  fetchDetail: (id: string, signal?: AbortSignal) => Promise<DocumentDetail>,
  callbacks: PollingCallbacks,
): void {
  const latestCallbacks = useRef(callbacks);
  latestCallbacks.current = callbacks;

  // `ids` is a new array on every render; the effect must only restart when the ids change.
  const idsKey = ids.join('|');

  useEffect(() => {
    const idList = idsKey ? idsKey.split('|') : [];
    if (idList.length === 0) return;

    let disposed = false;
    const timers = new Map<string, number>();
    const controllers = new Map<string, AbortController>();

    const clearTimer = (id: string): void => {
      const timer = timers.get(id);
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timers.delete(id);
      }
    };

    const schedule = (id: string, delayMs: number, failures: number): void => {
      clearTimer(id);
      const timer = window.setTimeout(() => {
        void tick(id, failures);
      }, delayMs);
      timers.set(id, timer);
    };

    const tick = async (id: string, failures: number): Promise<void> => {
      timers.delete(id);
      if (disposed) return;
      const controller = new AbortController();
      controllers.set(id, controller);
      try {
        const detail = await fetchDetail(id, controller.signal);
        controllers.delete(id);
        if (disposed) return;
        latestCallbacks.current.onDetail(detail);
        // Terminal documents are final: stop this chain here.
        if (isTerminal(detail.status)) return;
        schedule(id, POLL_INTERVAL_MS, 0);
      } catch (err: unknown) {
        controllers.delete(id);
        if (disposed) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        const status = err instanceof ApiError ? err.status : 0;
        const fatal = status === 404 || status === 410;
        const message = err instanceof Error && err.message ? err.message : 'Status check failed.';
        latestCallbacks.current.onError(id, message, fatal);
        if (fatal) return;
        const backoffMs = Math.min(POLL_INTERVAL_MS * 2 ** (failures + 1), MAX_BACKOFF_MS);
        schedule(id, backoffMs, failures + 1);
      }
    };

    for (const id of idList) {
      schedule(id, 0, 0);
    }

    return () => {
      disposed = true;
      for (const timer of timers.values()) {
        window.clearTimeout(timer);
      }
      timers.clear();
      for (const controller of controllers.values()) {
        controller.abort();
      }
      controllers.clear();
    };
  }, [idsKey, fetchDetail]);
}
