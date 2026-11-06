/**
 * Trusted client IPs. This module has no other omnilimit dependency, so it also works
 * next to `hono-rate-limiter`, `express-rate-limit` or anything else needing a client IP.
 */

/** Any way of reading a lowercase header name. */
export type HeaderSource =
  | { get(name: string): string | null }
  | Record<string, string | string[] | undefined>;

export type IpPreset = 'cloudflare' | 'cloud-run' | 'cloud-run-lb' | 'vercel' | 'direct';

export type IpStrategy =
  | { preset: IpPreset }
  /** Read `X-Forwarded-For` from the right, skipping `hops` entries added by your own proxies. */
  | { xff: { hops: number } }
  /** A single-IP header that your edge overwrites. Anyone reaching your origin directly can forge it. */
  | { header: string }
  /** The TCP peer address. */
  | { peer: true };

export interface IpInput {
  headers: HeaderSource;
  peer?: string | null | undefined;
}

const PRESETS: Record<IpPreset, Exclude<IpStrategy, { preset: IpPreset }>> = {
  cloudflare: { header: 'cf-connecting-ip' },
  'cloud-run': { xff: { hops: 0 } },
  'cloud-run-lb': { xff: { hops: 1 } },
  vercel: { header: 'x-vercel-forwarded-for' },
  direct: { peer: true },
};

function readHeader(headers: HeaderSource, name: string): string | null {
  if (typeof (headers as { get?: unknown }).get === 'function') {
    return (headers as { get(name: string): string | null }).get(name);
  }
  const value = (headers as Record<string, string | string[] | undefined>)[name];
  if (value === undefined) return null;
  return Array.isArray(value) ? value.join(',') : value;
}

function parseIpv4(raw: string): number[] | null {
  const parts = raw.split('.');
  if (parts.length !== 4) return null;
  const out: number[] = [];
  for (const part of parts) {
    if (!/^(0|[1-9][0-9]{0,2})$/.test(part)) return null;
    const value = Number(part);
    if (value > 255) return null;
    out.push(value);
  }
  return out;
}

/** Parse to eight 16-bit groups, or `null`. Zone identifiers are rejected. */
function parseIpv6(raw: string): number[] | null {
  if (raw.includes('%')) return null;
  let text = raw;
  const lastColon = text.lastIndexOf(':');
  if (text.includes('.')) {
    if (lastColon === -1) return null;
    const v4 = parseIpv4(text.slice(lastColon + 1));
    if (!v4) return null;
    const [a, b, c, d] = v4 as [number, number, number, number];
    text = `${text.slice(0, lastColon + 1)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = text.split('::');
  if (halves.length > 2) return null;
  const parseGroups = (part: string | undefined): number[] | null => {
    if (!part) return [];
    const groups: number[] = [];
    for (const group of part.split(':')) {
      if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return null;
      groups.push(Number.parseInt(group, 16));
    }
    return groups;
  };
  const head = parseGroups(halves[0]);
  const tail = parseGroups(halves[1]);
  if (!head || !tail) return null;
  if (halves.length === 1) return head.length === 8 ? head : null;
  const missing = 8 - head.length - tail.length;
  // `::` must stand for at least one group.
  if (missing < 1) return null;
  return [...head, ...new Array<number>(missing).fill(0), ...tail];
}

function formatIpv6(groups: number[]): string {
  let bestStart = -1;
  let bestLength = 0;
  let index = 0;
  while (index < groups.length) {
    if (groups[index] !== 0) {
      index += 1;
      continue;
    }
    let end = index;
    while (end < groups.length && groups[end] === 0) end += 1;
    if (end - index > bestLength) {
      bestStart = index;
      bestLength = end - index;
    }
    index = end;
  }
  const hex = groups.map((group) => group.toString(16));
  if (bestLength < 2) return hex.join(':');
  return `${hex.slice(0, bestStart).join(':')}::${hex.slice(bestStart + bestLength).join(':')}`;
}

/** Validate and normalise an address (RFC 5952; IPv4-mapped becomes IPv4). Returns `null` if invalid. */
export function canonicalIp(raw: string | null | undefined): string | null {
  if (!raw) return null;
  if (!raw.includes(':')) return parseIpv4(raw)?.join('.') ?? null;
  const groups = parseIpv6(raw);
  if (!groups) return null;
  const isMapped = groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff;
  if (isMapped) {
    const high = groups[6] as number;
    const low = groups[7] as number;
    return `${high >> 8}.${high & 0xff}.${low >> 8}.${low & 0xff}`;
  }
  return formatIpv6(groups);
}

/**
 * Keep the network prefix of an IPv6 address, since one customer commonly controls a whole
 * /64. IPv4 addresses are returned unchanged.
 */
export function maskIp(ip: string, prefix: number): string | null {
  const canonical = canonicalIp(ip);
  if (!canonical) return null;
  if (!canonical.includes(':')) return canonical;
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 128) {
    throw new RangeError('omnilimit: IPv6 prefix must be an integer from 0 to 128');
  }
  const groups = parseIpv6(canonical) as number[];
  const masked = groups.map((group, index) => {
    const bits = Math.min(16, Math.max(0, prefix - index * 16));
    return group & (bits === 0 ? 0 : (0xffff << (16 - bits)) & 0xffff);
  });
  return formatIpv6(masked);
}

function fromXff(value: string | null, hops: number): string | null {
  if (value === null) return null;
  const entries = value.split(',').map((entry) => entry.trim());
  // The client controls everything to the left of what your proxies appended, so a chain
  // that is too short must never fall back to its first value.
  if (entries.length <= hops) return null;
  return canonicalIp(entries[entries.length - 1 - hops]);
}

/**
 * Resolve the client IP using only sources your deployment makes trustworthy. Returns
 * `null` rather than guessing when the configured source is missing or malformed.
 */
export function resolveClientIp(input: IpInput, strategy: IpStrategy): string | null {
  const resolved = 'preset' in strategy ? PRESETS[strategy.preset] : strategy;
  if (!resolved) throw new RangeError('omnilimit: unknown IP preset');
  if ('xff' in resolved) {
    const { hops } = resolved.xff;
    if (!Number.isSafeInteger(hops) || hops < 0) throw new RangeError('omnilimit: hops must be a non-negative integer');
    return fromXff(readHeader(input.headers, 'x-forwarded-for'), hops);
  }
  if ('header' in resolved) return canonicalIp(readHeader(input.headers, resolved.header.toLowerCase())?.trim());
  return canonicalIp(input.peer);
}
