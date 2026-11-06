import { defineConfig } from 'tsup';

export default defineConfig({
  entry: {
    index: 'src/index.ts',
    ip: 'src/ip/index.ts',
    redis: 'src/stores/redis.ts',
    postgres: 'src/stores/postgres.ts',
    cloudflare: 'src/stores/cloudflare.ts',
    'cloudflare-do': 'src/stores/cloudflare-do.ts',
    hono: 'src/adapters/hono.ts',
    next: 'src/adapters/next.ts',
    express: 'src/adapters/express.ts',
  },
  format: ['esm', 'cjs'],
  dts: true,
  clean: true,
  splitting: true,
  external: ['cloudflare:workers'],
});
