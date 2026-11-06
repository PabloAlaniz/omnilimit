import type { ResolvedPolicy } from './gcra.js';

const encoder = new TextEncoder();
const HASH_HEX_LENGTH = 32;
const STATE_FORMAT = 1;

/**
 * Build the bucket key. The id is hashed so keys have bounded length and stores do not
 * hold raw IPs. The policy is part of the key because levels are measured in window
 * units: changing a limit starts a new counter instead of misreading the old one.
 */
export async function deriveKey(scope: string, policy: ResolvedPolicy, salt: string, id: string): Promise<string> {
  const saltBytes = encoder.encode(salt);
  const idBytes = encoder.encode(id);
  const input = new Uint8Array(saltBytes.length + 1 + idBytes.length);
  input.set(saltBytes, 0);
  input.set(idBytes, saltBytes.length + 1);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
  let hex = '';
  for (const byte of digest) hex += byte.toString(16).padStart(2, '0');
  const shape = `${policy.limit}-${policy.windowMs}-${policy.burst}`;
  return `ol:${STATE_FORMAT}:${scope}:${shape}:${hex.slice(0, HASH_HEX_LENGTH)}`;
}
