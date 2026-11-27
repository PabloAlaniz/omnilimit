import { createHash } from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';
import { resolvePolicy } from '../../src/core/gcra.js';
import { deriveKey, nodeHash, webHash } from '../../src/core/key.js';
import { createLimiter, memoryStore } from '../../src/index.js';
import { toPolicy, vectorsOf } from '../support/vectors.js';

const policy = resolvePolicy({ limit: 5, windowMs: 60_000 });
const sha256 = (text: string): string => createHash('sha256').update(text, 'utf8').digest('hex');
/** A runtime whose `node:crypto` is the given module. */
const runtimeWith = (crypto: unknown) => ({ process: { getBuiltinModule: () => crypto } });

describe('key hashes', () => {
  const native = nodeHash();

  it('finds the hash of node:crypto here, and it answers at once', () => {
    expect(native).not.toBeNull();
    expect(typeof deriveKey('t', policy, 'salt', 'id')).toBe('string');
    expect(deriveKey('t', policy, 'salt', 'id', webHash)).toBeInstanceOf(Promise);
  });

  it('derives the keys of the vectors on both paths', async () => {
    for (const vector of vectorsOf('key')) {
      for (const c of vector.cases) {
        const resolved = resolvePolicy(toPolicy(c.policy));
        expect(deriveKey(c.scope, resolved, c.salt, c.id, native!), JSON.stringify(c)).toBe(c.expect);
        expect(await deriveKey(c.scope, resolved, c.salt, c.id, webHash), JSON.stringify(c)).toBe(c.expect);
      }
    }
  });

  it('derives the same key on both paths, whatever the salt and the id', async () => {
    const texts = ['', 'a', '203.0.113.7', 'ñandú', '😀', 'a\0b', '\0', '\ud800', '\udc00', 'x\ud83d', '\ude00y', 'k'.repeat(300)];
    for (const salt of texts) {
      for (const id of texts) {
        const fast = deriveKey('t', policy, salt, id, native!);
        expect(fast, JSON.stringify([salt, id])).toBe(await deriveKey('t', policy, salt, id, webHash));
      }
    }
    // The separator is part of the input: the salt and the id cannot trade characters.
    expect(deriveKey('t', policy, 'ab', 'c', native!)).not.toBe(deriveKey('t', policy, 'a', 'bc', native!));
    expect(await deriveKey('t', policy, 'ab', 'c', webHash)).not.toBe(await deriveKey('t', policy, 'a', 'bc', webHash));
  });

  it('reads a missing id as the empty string on both paths, as an encoder does', async () => {
    const missing = undefined as unknown as string;
    expect(deriveKey('t', policy, 'salt', missing, native!)).toBe(deriveKey('t', policy, 'salt', '', native!));
    expect(await deriveKey('t', policy, 'salt', missing, webHash)).toBe(deriveKey('t', policy, 'salt', '', native!));
    expect(deriveKey('t', policy, 'salt', 7 as unknown as string, native!)).toBe(await deriveKey('t', policy, 'salt', '7', webHash));
  });

  it('uses createHash where node:crypto has no one-shot hash', async () => {
    const older = nodeHash(runtimeWith({ createHash }));
    expect(older).not.toBeNull();
    expect(deriveKey('t', policy, 'salt', 'ñ\ud800', older!)).toBe(await deriveKey('t', policy, 'salt', 'ñ\ud800', webHash));
  });

  it('falls back to WebCrypto where the runtime has no node:crypto, or one that hashes differently', () => {
    expect(nodeHash({})).toBeNull();
    expect(nodeHash({ process: {} })).toBeNull();
    expect(nodeHash(runtimeWith(undefined))).toBeNull();
    expect(nodeHash(runtimeWith({}))).toBeNull();
    expect(nodeHash({ process: { getBuiltinModule: () => { throw new Error('not here'); } } })).toBeNull();
    // Hashes UTF-16 code units, or drops what it cannot encode: not the bytes the spec names.
    expect(nodeHash(runtimeWith({ hash: (_a: string, data: string) => sha256(data.replace(/[\ud800-\udfff]/g, '')) }))).toBeNull();
    expect(nodeHash(runtimeWith({ hash: (_a: string, data: string) => sha256(data) }))).not.toBeNull();
  });

  it('counts on the same buckets from a runtime that has only WebCrypto', async () => {
    const real = process.getBuiltinModule.bind(process);
    const hidden = vi.spyOn(process, 'getBuiltinModule').mockImplementation(((id: string) => (id === 'node:crypto' ? undefined : real(id))) as never);
    vi.resetModules();
    try {
      // A fresh copy of the package, which looks for its hash again and finds none.
      const web = await import('../../src/index.js');
      const store = memoryStore();
      const options = { store, scope: 't', limit: 3, windowMs: 60_000, salt: 'pepper', onStoreError: 'closed' } as const;
      const [here, there] = [createLimiter(options), web.createLimiter(options)];
      expect((await there.check('ñ')).remaining).toBe(2);
      expect((await here.check('ñ')).remaining).toBe(1);
      expect((await there.peek('ñ')).remaining).toBe(1);
      const { reservation } = await there.reserve('ñ');
      expect((await here.peek('ñ')).remaining).toBe(0);
      expect(await there.refund(reservation)).toBe(true);
      expect((await here.peek('ñ')).remaining).toBe(1);
      expect(await there.reset('ñ')).toBe(true);
      expect((await here.peek('ñ')).remaining).toBe(3);
      expect(hidden).toHaveBeenCalledWith('node:crypto');
    } finally {
      hidden.mockRestore();
      vi.resetModules();
    }
  });
});
