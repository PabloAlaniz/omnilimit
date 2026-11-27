import type { ResolvedPolicy } from './gcra.js';

const encoder = new TextEncoder();
const HASH_HEX_LENGTH = 32;
const STATE_FORMAT = 1;

/**
 * `SHA-256(salt || 0x00 || id)` in UTF-8, as lowercase hex: at least its first 32 characters.
 * A string where the runtime can hash without waiting, a promise where it cannot.
 */
export type KeyHash = (salt: string, id: string) => string | Promise<string>;

/** WebCrypto: every runtime has it, and it only answers through a promise. */
export const webHash: KeyHash = async (salt, id) => {
  const saltBytes = encoder.encode(salt);
  const idBytes = encoder.encode(id);
  const input = new Uint8Array(saltBytes.length + 1 + idBytes.length);
  input.set(saltBytes, 0);
  input.set(idBytes, saltBytes.length + 1);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
  let hex = '';
  for (let i = 0; i < HASH_HEX_LENGTH / 2; i++) hex += (digest[i] as number).toString(16).padStart(2, '0');
  return hex;
};

// A salt and an id that an encoder could treat differently: a character of two bytes, one of
// four, and a lone surrogate on each side of the separator, which UTF-8 turns into U+FFFD.
const PROBE = ['ñ\ud800', '\udc00😀'] as const;
const PROBE_HASH = 'fdefcb35a0101347ba82e9aacafc4a54df3b13bd5ec7824c35bf3a2f6814c3ee';

interface NodeCrypto {
  hash?: (algorithm: string, data: string, outputEncoding: 'hex') => string;
  createHash?: (algorithm: string) => { update(data: string, inputEncoding: 'utf8'): { digest(encoding: 'hex'): string } };
}

/**
 * The hash of `node:crypto`, where the runtime has one: it answers at once, and a check on a
 * store in memory then costs a fraction of what the WebCrypto round trip does. The module is
 * asked of the runtime rather than imported, so a bundle for a runtime without it has nothing
 * to resolve. `null` when there is none, or when it does not hash the probe as the spec does.
 */
export function nodeHash(runtime: unknown = globalThis): KeyHash | null {
  try {
    const { process } = runtime as { process?: { getBuiltinModule?: (id: string) => NodeCrypto | undefined } };
    const crypto = process?.getBuiltinModule?.('node:crypto');
    let hash: KeyHash | null = null;
    if (typeof crypto?.hash === 'function') {
      const oneShot = crypto.hash;
      hash = (salt, id) => oneShot('sha256', `${salt}\0${id}`, 'hex');
    } else if (typeof crypto?.createHash === 'function') {
      const createHash = crypto.createHash;
      hash = (salt, id) => createHash('sha256').update(`${salt}\0${id}`, 'utf8').digest('hex');
    }
    return hash?.(...PROBE) === PROBE_HASH ? hash : null;
  } catch {
    return null;
  }
}

let runtimeHash: KeyHash | undefined;

/** Derive the keys of one limiter. Its scope, policy and salt never change, so they are joined once. */
export function keyDeriver(
  scope: string,
  policy: ResolvedPolicy,
  salt: string,
  hash: KeyHash = (runtimeHash ??= nodeHash() ?? webHash),
): (id: string) => string | Promise<string> {
  const shape = `${policy.limit}-${policy.windowMs}-${policy.burst}`;
  const prefix = `ol:${STATE_FORMAT}:${scope}:${shape}:`;
  const finish = (hex: string): string => prefix + hex.slice(0, HASH_HEX_LENGTH);
  return (id) => {
    // A caller without types can pass no id at all. An encoder reads that as the empty string,
    // and so does every hash here.
    const hex = hash(salt, id === undefined ? '' : id);
    return typeof hex === 'string' ? finish(hex) : hex.then(finish);
  };
}

/**
 * Build the bucket key. The id is hashed so keys have bounded length and stores do not
 * hold raw IPs. The policy is part of the key because levels are measured in window
 * units: changing a limit starts a new counter instead of misreading the old one.
 */
export function deriveKey(scope: string, policy: ResolvedPolicy, salt: string, id: string, hash?: KeyHash): string | Promise<string> {
  return keyDeriver(scope, policy, salt, hash)(id);
}
