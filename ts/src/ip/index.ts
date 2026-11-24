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
  /** Read the standard `Forwarded` header (RFC 7239) from the right, with the same hop count rule. */
  | { forwarded: { hops: number } }
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

/**
 * The bucket id for a client: its trusted IP with IPv6 grouped by prefix, or `null` when the
 * configured source has no valid address. It is `resolveClientIp` followed by `maskIp`, which
 * is what a hand-written key function needs: `clientKey(input, options) ?? 'shared'`.
 */
export function clientKey(input: IpInput, options: IpStrategy & { ipv6Prefix?: number }): string | null {
  const { ipv6Prefix = 64, ...strategy } = options;
  const ip = resolveClientIp(input, strategy as IpStrategy);
  return ip === null ? null : maskIp(ip, ipv6Prefix);
}

/**
 * Remove the optional whitespace of HTTP, spaces and tabs (RFC 9110). `String.prototype.trim`
 * removes more, and not the same characters as other languages do, so ports would disagree.
 */
function trimOws(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && (value[start] === ' ' || value[start] === '\t')) start += 1;
  while (end > start && (value[end - 1] === ' ' || value[end - 1] === '\t')) end -= 1;
  return value.slice(start, end);
}

function fromXff(value: string | null, hops: number): string | null {
  if (value === null) return null;
  const entries = value.split(',').map(trimOws);
  // The client controls everything to the left of what your proxies appended, so a chain
  // that is too short must never fall back to its first value.
  if (entries.length <= hops) return null;
  return canonicalIp(entries[entries.length - 1 - hops]);
}

const PORT = /^[0-9]{1,5}$/;

/** The address of a `for` value of RFC 7239: IPv4 or bracketed IPv6, with an optional port. */
function forwardedNode(value: string): string | null {
  let node = value;
  if (node.startsWith('"')) {
    if (node.length < 2 || !node.endsWith('"')) return null;
    node = node.slice(1, -1);
    if (node.includes('"') || node.includes('\\')) return null;
  }
  if (node.startsWith('[')) {
    const end = node.indexOf(']');
    if (end === -1) return null;
    const host = node.slice(1, end);
    const rest = node.slice(end + 1);
    if (!host.includes(':')) return null;
    if (rest !== '' && !(rest.startsWith(':') && PORT.test(rest.slice(1)))) return null;
    return canonicalIp(host);
  }
  const colon = node.indexOf(':');
  if (colon === -1) return canonicalIp(node);
  return PORT.test(node.slice(colon + 1)) ? canonicalIp(node.slice(0, colon)) : null;
}

function fromForwarded(value: string | null, hops: number): string | null {
  if (value === null) return null;
  // Every comma, even inside a quoted string: a quote the client opened must not be able to
  // swallow the element your proxy appended. No address contains a comma or a semicolon.
  const elements = value.split(',');
  if (elements.length <= hops) return null;
  let node: string | null = null;
  for (const raw of (elements[elements.length - 1 - hops] as string).split(';')) {
    const pair = trimOws(raw);
    if (pair === '') continue;
    const equals = pair.indexOf('=');
    if (equals === -1) return null;
    if (pair.slice(0, equals).toLowerCase() !== 'for') continue;
    if (node !== null) return null;
    node = pair.slice(equals + 1);
  }
  return node === null ? null : forwardedNode(node);
}

const assertHops = (hops: unknown): void => {
  if (!Number.isSafeInteger(hops) || (hops as number) < 0) throw new RangeError('omnilimit: hops must be a non-negative integer');
};

/**
 * Resolve the client IP using only sources your deployment makes trustworthy. Returns
 * `null` rather than guessing when the configured source is missing or malformed.
 */
export function resolveClientIp(input: IpInput, strategy: IpStrategy): string | null {
  const resolved = 'preset' in strategy ? PRESETS[strategy.preset] : strategy;
  if (!resolved) throw new RangeError('omnilimit: unknown IP preset');
  if ('xff' in resolved) {
    assertHops(resolved.xff.hops);
    return fromXff(readHeader(input.headers, 'x-forwarded-for'), resolved.xff.hops);
  }
  if ('forwarded' in resolved) {
    assertHops(resolved.forwarded.hops);
    return fromForwarded(readHeader(input.headers, 'forwarded'), resolved.forwarded.hops);
  }
  if ('header' in resolved) {
    const value = readHeader(input.headers, resolved.header.toLowerCase());
    return canonicalIp(value === null ? null : trimOws(value));
  }
  return canonicalIp(input.peer);
}

export type IpWarningCode = 'source_missing' | 'chain_too_short' | 'not_an_address' | 'not_public' | 'forwarding_headers_ignored';

export interface IpWarning {
  /** Stable, and the same in every port: see "Explaining a resolution" in the spec. */
  code: IpWarningCode;
  /** For a person to read. The wording may change between releases. */
  message: string;
}

export interface ClientIpReport {
  /** What `resolveClientIp` returns for the same input and strategy. */
  ip: string | null;
  warnings: IpWarning[];
}

const WARNINGS: Record<IpWarningCode, string> = {
  source_missing: 'the header or peer address this strategy reads is absent: the request did not come through the proxy you configured for',
  chain_too_short: 'the forwarding chain has no entry at this hop count: hops is higher than the number of proxies that append to it',
  not_an_address: 'the value in the trusted position is not a valid address',
  not_public:
    'the address resolved is private, loopback or link-local: it is most likely one of your own proxies, so every client behind it shares one bucket. Raise hops, or read the header your edge sets',
  forwarding_headers_ignored:
    'the TCP peer is read, but the request carries a forwarding header: a proxy is in front, and the peer is that proxy, not the client',
};

function isNotPublic(ip: string): boolean {
  if (!ip.includes(':')) {
    const [a, b] = ip.split('.').map(Number) as [number, number];
    return (
      a === 0 || a === 10 || a === 127 || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
    );
  }
  const groups = parseIpv6(ip) as number[];
  const first = groups[0] as number;
  const unspecifiedOrLoopback = groups.slice(0, 7).every((group) => group === 0) && (groups[7] as number) <= 1;
  return unspecifiedOrLoopback || (first & 0xfe00) === 0xfc00 || (first & 0xffc0) === 0xfe80;
}

/**
 * The address a strategy resolves, with what looks wrong about the configuration. For a startup
 * check, a test or a debug route: it never changes what `resolveClientIp` returns, and it has
 * no place on the request path.
 *
 * ```ts
 * const { ip, warnings } = explainClientIp({ headers: req.headers, peer }, { xff: { hops: 1 } });
 * for (const warning of warnings) log.warn(`client ip: ${warning.code}: ${warning.message}`);
 * ```
 */
export function explainClientIp(input: IpInput, strategy: IpStrategy): ClientIpReport {
  const ip = resolveClientIp(input, strategy);
  const resolved = 'preset' in strategy ? PRESETS[strategy.preset] : strategy;
  const codes: IpWarningCode[] = [];
  if (ip === null) {
    if ('xff' in resolved || 'forwarded' in resolved) {
      const [name, hops] = 'xff' in resolved ? ['x-forwarded-for', resolved.xff.hops] : ['forwarded', resolved.forwarded.hops];
      const value = readHeader(input.headers, name as string);
      if (value === null) codes.push('source_missing');
      else codes.push(value.split(',').length <= (hops as number) ? 'chain_too_short' : 'not_an_address');
    } else if ('header' in resolved) {
      codes.push(readHeader(input.headers, resolved.header.toLowerCase()) === null ? 'source_missing' : 'not_an_address');
    } else {
      codes.push(input.peer ? 'not_an_address' : 'source_missing');
    }
  } else if (isNotPublic(ip)) {
    codes.push('not_public');
  }
  if ('peer' in resolved && (readHeader(input.headers, 'x-forwarded-for') !== null || readHeader(input.headers, 'forwarded') !== null)) {
    codes.push('forwarding_headers_ignored');
  }
  return { ip, warnings: codes.map((code) => ({ code, message: WARNINGS[code] })) };
}
