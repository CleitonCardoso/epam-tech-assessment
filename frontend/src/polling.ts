import { ApiError } from './api';
import { isTerminal, type DocumentStatus } from './types';

/** Per the delivery requirements: poll non-terminal documents every 1.5 s. */
export const POLL_INTERVAL_MS = 1500;

/** On HTTP errors back off 3 s -> 6 s -> 12 s -> ... capped here. */
export const MAX_BACKOFF_MS = 30_000;

/**
 * Delay before retry number `failures + 1`: the first HTTP failure waits 3 s, each further
 * failure doubles the wait, and the cap keeps a broken API from being hammered forever.
 * Pure so the policy can be tested without fake timers.
 */
export function nextDelayMs(failures: number): number {
  const safeFailures = Number.isFinite(failures) ? Math.max(0, Math.floor(failures)) : 0;
  return Math.min(POLL_INTERVAL_MS * 2 ** (safeFailures + 1), MAX_BACKOFF_MS);
}

/** Terminal statuses are final: their polling chain must stop instead of rescheduling. */
export function shouldContinue(status: DocumentStatus): boolean {
  return !isTerminal(status);
}

/** 404/410 mean the document will never appear again: stop instead of retrying forever. */
export function isFatalPollError(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  return err.status === 404 || err.status === 410;
}
