// Runs in real workerd, without nodejs_compat, against the built package.
import { checkAll, createLimiter, createLockout, memoryStore, reserveAll } from '../../dist/index.js';
import type { Store } from '../../dist/index.js';
import { runRecipeConcurrency } from '../support/concurrency.js';
import { cloudflareBindingStore } from '../../dist/cloudflare.js';
import { durableObjectStore, OmnilimitDurableObject } from '../../dist/cloudflare-do.js';
import { resolveClientIp } from '../../dist/ip.js';

export { OmnilimitDurableObject };

interface Env {
  OMNI: never;
  RL: { limit(options: { key: string }): Promise<{ success: boolean }> };
}

interface Body {
  op:
    | 'sequence'
    | 'concurrency'
    | 'recipe-concurrency'
    | 'mixed-concurrency'
    | 'reserve-refund'
    | 'reserve-all'
    | 'levels'
    | 'binding'
    | 'ip'
    | 'real-clock'
    | 'real-reset'
    | 'real-refund';
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
      type Step = { id: string; at_ms: number; op?: 'peek' | 'reset' | 'refund'; cost?: number; of?: number };
      const steps = body['steps'] as Step[];
      const refunded = new Set(steps.filter((step) => step.op === 'refund').map((step) => step.of));
      const reservations = new Map<number, Awaited<ReturnType<typeof limiter.reserve>>['reservation']>();
      for (const [index, step] of steps.entries()) {
        const options = { nowMs: step.at_ms, cost: step.cost ?? 1 };
        if (step.op === 'reset') out.push({ reset: await limiter.reset(step.id) });
        else if (step.op === 'refund') out.push({ refunded: await limiter.refund(reservations.get(step.of!) ?? null, { nowMs: step.at_ms }) });
        else if (step.op === 'peek') out.push(await limiter.peek(step.id, options));
        else if (refunded.has(index)) {
          const { decision, reservation } = await limiter.reserve(step.id, options);
          reservations.set(index, reservation);
          out.push(decision);
        } else out.push(await limiter.check(step.id, options));
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
    if (body.op === 'recipe-concurrency') {
      // The recipe form of a concurrency vector, one Durable Object per bucket.
      const vector = body['vector'] as { salt: string };
      return Response.json(await runRecipeConcurrency({ createLimiter, checkAll, reserveAll, createLockout }, vector, store, body['salt'] as string));
    }
    // One request of many sent at once: every store call at the same time, so a refund gives back
    // all it took and the levels count exactly what stays reserved or committed.
    const at = body['at_ms'] as number;
    const fixed: Store = {
      tier: store.tier,
      hit: (key, params) => store.hit(key, params, at),
      peek: (key, params) => store.peek!(key, params, at),
      reset: (key) => store.reset!(key),
      refund: (key, params, sinceMs) => store.refund!(key, params, sinceMs, at),
    };
    // Limit 1: a peek's `resetMs` is the level itself.
    const bucket = (scope: string, burst: number) =>
      createLimiter({ limit: 1, windowMs: 60_000, burst, store: fixed, scope, salt: body['salt'] as string, onStoreError: 'closed', storeTimeoutMs: 30_000 });
    if (body.op === 'reserve-refund') {
      const limiter = bucket('r', 3);
      const { decision, reservation } = await limiter.reserve('a');
      const refunded = body['keep'] ? false : await limiter.refund(reservation);
      return Response.json({ allowed: decision.allowed, kept: decision.allowed && !refunded, error: decision.reason === 'store_error' });
    }
    if (body.op === 'reserve-all') {
      const pending = await reserveAll([
        { limiter: bucket('a', 4), id: 'x' },
        { limiter: bucket('b', 3), id: 'y' },
      ]);
      const errors = pending.decisions.filter((d) => d.reason === 'store_error').length;
      if (pending.allowed && body['keep']) {
        await pending.commit();
        return Response.json({ allowed: true, kept: true, errors });
      }
      const { refunded } = await pending.cancel();
      return Response.json({ allowed: pending.allowed, kept: false, errors: errors + refunded.filter((done) => done === false).length });
    }
    if (body.op === 'levels') {
      const levels: Record<string, number | null> = {};
      for (const [scope, burst, id] of body['buckets'] as [string, number, string][]) levels[scope] = (await bucket(scope, burst).peek(id)).resetMs;
      return Response.json(levels);
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
    if (body.op === 'real-refund') {
      // On the object's own clock, which also sets the alarm: a refund at once frees the unit.
      const limiter = createLimiter({ limit: 1, windowMs: 60_000, store, scope: 'real', salt: body['salt'] as string, onStoreError: 'closed' });
      const { reservation } = await limiter.reserve('a');
      const denied = await limiter.peek('a');
      const refunded = await limiter.refund(reservation);
      return Response.json({ denied, refunded, again: await limiter.refund(reservation), after: await limiter.check('a') });
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
