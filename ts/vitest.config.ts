import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    testTimeout: 30_000,
    coverage: {
      provider: 'v8',
      include: ['src/core/**', 'src/ip/**', 'src/stores/memory.ts', 'src/adapters/**'],
      thresholds: { lines: 95, functions: 95, statements: 95, branches: 90 },
    },
  },
});
