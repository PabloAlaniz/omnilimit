// Runs in real workerd, without nodejs_compat, against the built package.
import { createLimiter, memoryStore } from '../../dist/index.js';
import { cloudflareBindingStore } from '../../dist/cloudflare.js';
import { durableObjectStore, OmnilimitDurableObject } from '../../dist/cloudflare-do.js';
import { resolveClientIp } from '../../dist/ip.js';

export { OmnilimitDurableObject };

interface Env {
  OMNI: never;
  RL: { limit(options: { key: string }): Promise<{ success: boolean }> };
}

interface Body {
  op: 'sequence' | 'concurrency' | 'binding' | 'ip' | 'real-clock';
  store?: 'memory' | 'do';
  [name: string]: unknown;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const body = (await request.json()) as Body;
    const store = body.store === 'do' ? durableObjectStore(env.OMNI) : memoryStore();
    const options = (policy: Record<string, number>, salt: string) => ({
      limit: policy['limit']!,
      windowMs: policy['window_ms']!,
      burst: policy['burst']!,
      store,
      scope: (body['scope'] as string) ?? 't',
      salt,
      onStoreError: 'closed' as const,
      storeTimeoutMs: 30_000,
    });

    if (body.op === 'sequence') {
      const limiter = createLimiter(options(body['policy'] as never, body['salt'] as string));
      const out = [];
      for (const step of body['steps'] as Array<{ id: string; at_ms: number; op?: 'peek' | 'reset' }>) {
        if (step.op === 'reset') out.push({ reset: await limiter.reset(step.id) });
        else if (step.op === 'peek') out.push(await limiter.peek(step.id, { nowMs: step.at_ms }));
        else out.push(await limiter.check(step.id, { nowMs: step.at_ms }));
      }
      return Response.json(out);
    }
    if (body.op === 'concurrency') {
      const limiter = createLimiter(options(body['policy'] as never, body['salt'] as string));
      const results = await Promise.all(
        Array.from({ length: body['hits'] as number }, () =>
          limiter.check(body['id_value'] as string, { nowMs: body['at_ms'] as number }),
        ),
      );
      return Response.json({ allowed: results.filter((r) => r.allowed).length, errors: results.filter((r) => r.reason === 'store_error').length });
    }
    if (body.op === 'mixed-concurrency') {
      // Peeks interleaved with hits: only hits may take quota, and exactly `burst` of them.
      const limiter = createLimiter(options(body['policy'] as never, body['salt'] as string));
      const id = body['id_value'] as string;
      const nowMs = body['at_ms'] as number;
      const results = await Promise.all(
        Array.from({ length: body['hits'] as number }, (_, i) =>
          i % 2 === 0 ? limiter.peek(id, { nowMs }).then(() => null) : limiter.check(id, { nowMs }),
        ),
      );
      const checks = results.filter((r) => r !== null);
      return Response.json({ allowed: checks.filter((r) => r.allowed).length, errors: checks.filter((r) => r.reason === 'store_error').length });
    }
    if (body.op === 'real-clock') {
      const limiter = createLimiter({ limit: 1, windowMs: 1000, store, scope: 'real', salt: body['salt'] as string, onStoreError: 'closed' });
      return Response.json([await limiter.check('a'), await limiter.check('a')]);
    }
    if (body.op === 'real-reset') {
      const limiter = createLimiter({ limit: 1, windowMs: 60_000, store, scope: 'real', salt: body['salt'] as string, onStoreError: 'closed' });
      const first = await limiter.check('a');
      const denied = await limiter.peek('a');
      const reset = await limiter.reset('a');
      return Response.json({ first, denied, reset, after: await limiter.check('a') });
    }
    if (body.op === 'binding') {
      const limiter = createLimiter({
        limit: 2,
        windowMs: 60_000,
        store: cloudflareBindingStore({ binding: env.RL, periodSec: 60 }),
        scope: 'bind',
        onStoreError: 'closed',
      });
      return Response.json([await limiter.check('a'), await limiter.check('a'), await limiter.check('a')]);
    }
    const ipCase = body['case'] as { headers: Record<string, string>; peer: string | null; strategy: never };
    return Response.json({ ip: resolveClientIp({ headers: new Headers(ipCase.headers), peer: ipCase.peer }, ipCase.strategy) });
  },
};
