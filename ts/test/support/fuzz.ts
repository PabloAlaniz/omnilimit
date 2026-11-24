// Vectors drawn for this run by `tools/fuzz_vectors.py`, when OMNILIMIT_FUZZ_VECTORS names their
// directory. They have the shape of the committed ones and go through the same runners.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import type { Vector } from './vectors.js';

const dir = process.env.OMNILIMIT_FUZZ_VECTORS ? resolve(process.env.OMNILIMIT_FUZZ_VECTORS) : null;

function walk(path: string): string[] {
  return readdirSync(path).flatMap((name) => {
    const child = join(path, name);
    return statSync(child).isDirectory() ? walk(child) : child.endsWith('.json') ? [child] : [];
  });
}

if (dir && !existsSync(dir)) throw new Error(`OMNILIMIT_FUZZ_VECTORS: ${dir} does not exist; run tools/fuzz_vectors.py`);

export const fuzzSeed: string | null = dir ? readFileSync(join(dir, 'SEED'), 'utf8').trim() : null;
export const fuzzVectors: Vector[] = dir ? walk(dir).sort().map((path) => JSON.parse(readFileSync(path, 'utf8'))) : [];
export const fuzzOf = (kind: string): Vector[] => fuzzVectors.filter((vector) => vector.kind === kind);
/** Put the seed in a failure message: it is what reproduces the run. */
export const withSeed = (context: string) => `seed ${fuzzSeed}: ${context}`;
