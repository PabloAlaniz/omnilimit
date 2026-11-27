// Properties of the core that hold for every policy and every schedule, checked by fast-check on
// inputs it draws and shrinks. The vectors pin exact results; these pin the invariants.
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';
import { applyHit, applyPeek, applyRefund, parsePolicy, resolvePolicy, viewOf, type MeterState, type ResolvedPolicy } from '../../src/core/gcra.js';
import { canonicalIp, maskIp } from '../../src/ip/index.js';

const policyArb: fc.Arbitrary<ResolvedPolicy> = fc
  .record({
    limit: fc.integer({ min: 1, max: 1000 }),
    windowMs: fc.integer({ min: 1000, max: 86_400_000 }),
    burst: fc.option(fc.integer({ min: 1, max: 1000 }), { nil: undefined }),
  })
  .map(({ burst, ...policy }) => resolvePolicy(burst === undefined ? policy : { ...policy, burst }));

/** Gaps between calls, in milliseconds: often zero, sometimes long. */
const gapsArb = fc.array(fc.oneof(fc.constant(0), fc.integer({ min: 0, max: 5000 }), fc.integer({ min: 0, max: 200_000_000 })), {
  minLength: 1,
  maxLength: 200,
});

describe('GCRA properties', () => {
  it('allows exactly the burst at one instant, from an empty meter', () => {
    fc.assert(
      fc.property(policyArb, fc.integer({ min: 0, max: 2 ** 40 }), (params, now) => {
        let state: MeterState | null = null;
        let allowed = 0;
        for (let i = 0; i < params.burst + 3; i++) {
          const { hit, write } = applyHit(state, params, now);
          if (hit.allowed) allowed++;
          state = write ?? state;
        }
        expect(allowed).toBe(params.burst);
      }),
    );
  });

  it('keeps the level within capacity, never writes on a denial, and matches peek', () => {
    fc.assert(
      fc.property(policyArb, gapsArb, (params, gaps) => {
        let state: MeterState | null = null;
        let now = 0;
        for (const gap of gaps) {
          now += gap;
          const peek = applyPeek(state, params, now);
          const { hit, write } = applyHit(state, params, now);
          expect(hit.allowed).toBe(peek.allowed);
          expect(hit.level).toBeGreaterThanOrEqual(0);
          expect(hit.level).toBeLessThanOrEqual(params.capacity);
          if (!hit.allowed) expect(write).toBeNull();
          state = write ?? state;
        }
      }),
    );
  });

  it('never allows more than the burst plus what drained over the interval', () => {
    fc.assert(
      fc.property(policyArb, gapsArb, (params, gaps) => {
        let state: MeterState | null = null;
        let now = 0;
        let allowed = 0;
        for (const gap of gaps) {
          now += gap;
          const { hit, write } = applyHit(state, params, now);
          if (hit.allowed) allowed++;
          state = write ?? state;
        }
        // Every allowed hit adds windowMs; the meter holds at most capacity and drains limit per ms.
        expect(allowed * params.windowMs).toBeLessThanOrEqual(params.capacity + now * params.limit);
      }),
    );
  });

  it('a refund never raises the level and never goes below zero', () => {
    fc.assert(
      fc.property(policyArb, gapsArb, fc.integer({ min: 0, max: 10_000 }), (params, gaps, later) => {
        let state: MeterState | null = null;
        let now = 0;
        let since = 0;
        for (const gap of gaps) {
          now += gap;
          const { hit, write } = applyHit(state, params, now);
          if (hit.allowed) since = now;
          state = write ?? state;
        }
        const before = applyPeek(state, params, now + later).level;
        const { refund } = applyRefund(state, params, since, now + later);
        expect(refund.level).toBeGreaterThanOrEqual(0);
        expect(refund.level).toBeLessThanOrEqual(before);
      }),
    );
  });

  it('reports remaining within the burst, and a positive retry-after when denied', () => {
    fc.assert(
      fc.property(policyArb, fc.boolean(), fc.double({ min: 0, max: 1, noNaN: true }), (params, allowed, fraction) => {
        const level = Math.floor(params.capacity * fraction);
        const view = viewOf(allowed, level, params);
        expect(view.remaining).toBeGreaterThanOrEqual(0);
        expect(view.remaining).toBeLessThanOrEqual(params.burst);
        expect(view.resetMs).toBeGreaterThanOrEqual(0);
        if (allowed) expect(view.retryAfterMs).toBeNull();
        else if (level + params.windowMs > params.capacity) expect(view.retryAfterMs).toBeGreaterThan(0);
      }),
    );
  });
});

describe('policy shorthand properties', () => {
  const units = { second: 1000, minute: 60_000, hour: 3_600_000, day: 86_400_000 } as const;

  it('reads back what it was written from', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 100_000 }),
        fc.option(fc.integer({ min: 1, max: 365 }), { nil: undefined }),
        fc.constantFrom(...(Object.keys(units) as (keyof typeof units)[])),
        (limit, count, unit) => {
          const text = `${limit}/${count ?? ''}${unit}`;
          expect(parsePolicy(text)).toEqual({ limit, windowMs: (count ?? 1) * units[unit] });
        },
      ),
    );
  });

  it('only ever throws a RangeError, on any string', () => {
    fc.assert(
      fc.property(fc.string(), (text) => {
        try {
          parsePolicy(text);
        } catch (error) {
          expect(error).toBeInstanceOf(RangeError);
        }
      }),
    );
  });
});

describe('IP properties', () => {
  it('canonical form is stable and masking at full length keeps it', () => {
    fc.assert(
      fc.property(fc.oneof(fc.ipV4(), fc.ipV6()), (raw) => {
        const canonical = canonicalIp(raw);
        expect(canonical).not.toBeNull();
        expect(canonicalIp(canonical)).toBe(canonical);
        expect(maskIp(raw, 128)).toBe(canonical);
      }),
    );
  });

  it('a shorter IPv6 prefix groups every address a longer one groups together', () => {
    fc.assert(
      fc.property(fc.ipV6(), fc.integer({ min: 0, max: 128 }), fc.integer({ min: 0, max: 128 }), (raw, a, b) => {
        const [short, long] = a <= b ? [a, b] : [b, a];
        const masked = maskIp(raw, long) as string;
        expect(maskIp(masked, short)).toBe(maskIp(raw, short));
      }),
    );
  });

  it('never throws on arbitrary input', () => {
    fc.assert(
      fc.property(fc.string(), (raw) => {
        const canonical = canonicalIp(raw);
        if (canonical !== null) expect(canonicalIp(canonical)).toBe(canonical);
      }),
    );
  });
});
