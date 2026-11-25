import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import {
  isFatalPollError,
  MAX_BACKOFF_MS,
  nextDelayMs,
  POLL_INTERVAL_MS,
  shouldContinue,
} from './polling';
import { DOCUMENT_STATUSES, isTerminal } from './types';

describe('polling policy', () => {
  it('polls non-terminal documents every 1.5 s', () => {
    expect(POLL_INTERVAL_MS).toBe(1500);
  });

  it('grows the retry delay from 3 s by doubling', () => {
    expect(nextDelayMs(0)).toBe(3000);
    expect(nextDelayMs(1)).toBe(6000);
    expect(nextDelayMs(2)).toBe(12000);
    expect(nextDelayMs(3)).toBe(24000);
  });

  it('caps the retry delay at MAX_BACKOFF_MS and never decreases', () => {
    expect(MAX_BACKOFF_MS).toBe(30_000);
    // 1.5 s * 2^5 = 48 s, which must be capped rather than kept.
    expect(nextDelayMs(4)).toBe(MAX_BACKOFF_MS);
    expect(nextDelayMs(50)).toBe(MAX_BACKOFF_MS);

    const delays = Array.from({ length: 12 }, (_, failures) => nextDelayMs(failures));
    expect(delays[0]).toBeGreaterThan(POLL_INTERVAL_MS);
    for (let index = 1; index < delays.length; index += 1) {
      expect(delays[index]).toBeGreaterThanOrEqual(delays[index - 1]);
    }
  });

  it('normalises nonsense failure counts instead of producing NaN or a shorter wait', () => {
    expect(nextDelayMs(-3)).toBe(3000);
    expect(nextDelayMs(1.7)).toBe(6000);
    expect(nextDelayMs(Number.NaN)).toBe(3000);
    expect(nextDelayMs(Number.POSITIVE_INFINITY)).toBe(3000);
  });

  it('stops polling exactly on terminal statuses and keeps polling the rest', () => {
    const decision = Object.fromEntries(
      DOCUMENT_STATUSES.map((status) => [status, shouldContinue(status)]),
    );
    expect(decision).toEqual({
      queued: true,
      parsing: true,
      summarizing: true,
      retrying: true,
      completed: false,
      failed: false,
    });
    for (const status of DOCUMENT_STATUSES) {
      expect(shouldContinue(status)).toBe(!isTerminal(status));
    }
  });

  it('treats only 404/410 as fatal polling errors', () => {
    expect(isFatalPollError(new ApiError(404, 'gone', null))).toBe(true);
    expect(isFatalPollError(new ApiError(410, 'gone', null))).toBe(true);
    expect(isFatalPollError(new ApiError(500, 'boom', null))).toBe(false);
    expect(isFatalPollError(new ApiError(503, 'down', null))).toBe(false);
    expect(isFatalPollError(new TypeError('Failed to fetch'))).toBe(false);
    expect(isFatalPollError('nope')).toBe(false);
  });
});
