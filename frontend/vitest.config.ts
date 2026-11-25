import { defineConfig } from 'vitest/config';

// The suite is mostly pure unit tests (polling policy, status mapping, API error parsing, list
// mapping), so the default `node` environment is enough. The one App-level test that proves
// rehydration on reload opts into jsdom with a per-file `@vitest-environment jsdom` comment.
// Tests live next to the module they cover and are type-checked by `tsc --noEmit` as well.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
