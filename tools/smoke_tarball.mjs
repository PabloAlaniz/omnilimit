#!/usr/bin/env node
// Install the packed tarball into clean projects: what is published must load in CJS,
// in ESM, and in workerd without nodejs_compat. `dist` alone would not catch packaging bugs.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const ts = join(root, 'ts');
const work = mkdtempSync(join(tmpdir(), 'omnilimit-smoke-'));
const run = (command, args, cwd) => execFileSync(command, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });

const NODE_SUBPATHS = ['omnilimit', 'omnilimit/ip', 'omnilimit/redis', 'omnilimit/postgres', 'omnilimit/cloudflare', 'omnilimit/hono', 'omnilimit/next', 'omnilimit/express'];

try {
  run('pnpm', ['pack', '--pack-destination', work], ts);
  const tarball = readdirSync(work).find((name) => name.endsWith('.tgz'));
  if (!tarball) throw new Error('no tarball produced');
  const listing = run('tar', ['-tzf', join(work, tarball)], work);
  for (const required of ['package/README.md', 'package/LICENSE', 'package/dist/index.js', 'package/dist/index.cjs']) {
    if (!listing.includes(required)) throw new Error(`tarball is missing ${required}`);
  }
  if (/package\/(src|test)\//.test(listing)) throw new Error('tarball includes source or tests');

  writeFileSync(join(work, 'package.json'), '{"name":"smoke","private":true}');
  run('npm', ['install', '--silent', '--no-audit', '--no-fund', join(work, tarball)], work);

  writeFileSync(
    join(work, 'smoke.cjs'),
    `${NODE_SUBPATHS.map((p) => `require(${JSON.stringify(p)});`).join('\n')}
const { createLimiter, memoryStore } = require('omnilimit');
const limiter = createLimiter({ limit: 1, windowMs: 1000, store: memoryStore(), scope: 'cjs', onStoreError: 'closed' });
limiter.check('a').then(() => limiter.check('a')).then((d) => { if (d.status !== 429) throw new Error('cjs'); console.log('cjs ok'); });`,
  );
  console.log(run('node', ['smoke.cjs'], work).trim());

  writeFileSync(
    join(work, 'smoke.mjs'),
    `${NODE_SUBPATHS.map((p, i) => `import * as m${i} from ${JSON.stringify(p)};`).join('\n')}
void [${NODE_SUBPATHS.map((_, i) => `m${i}`).join(', ')}];
const limiter = m0.createLimiter({ limit: 1, windowMs: 1000, store: m0.memoryStore(), scope: 'esm', onStoreError: 'closed' });
await limiter.check('a');
if ((await limiter.check('a')).status !== 429) throw new Error('esm');
console.log('esm ok');`,
  );
  console.log(run('node', ['smoke.mjs'], work).trim());

  writeFileSync(
    join(work, 'worker.mjs'),
    `import { createLimiter } from 'omnilimit';
import { durableObjectStore, OmnilimitDurableObject } from 'omnilimit/cloudflare/do';
import { ipKey, rateLimit } from 'omnilimit/hono';
export { OmnilimitDurableObject };
export default { async fetch(request, env) {
  const limiter = createLimiter({ limit: 1, windowMs: 1000, store: durableObjectStore(env.OMNI), scope: 'w', salt: crypto.randomUUID(), onStoreError: 'closed' });
  await limiter.check('a');
  void rateLimit; void ipKey;
  return Response.json(await limiter.check('a'));
} };`,
  );
  const require_ = await import(pathToFileURL(join(ts, 'node_modules/esbuild/lib/main.js')).href);
  const bundle = await require_.build({ entryPoints: [join(work, 'worker.mjs')], bundle: true, format: 'esm', platform: 'neutral', mainFields: ['module', 'main'], conditions: ['workerd', 'worker', 'import'], external: ['cloudflare:workers'], write: false, absWorkingDir: work });
  const { Miniflare } = await import(pathToFileURL(join(ts, 'node_modules/miniflare/dist/src/index.js')).href);
  const mf = new Miniflare({ modules: true, script: bundle.outputFiles[0].text, compatibilityDate: '2026-07-01', durableObjects: { OMNI: { className: 'OmnilimitDurableObject', useSQLite: true } } });
  const decision = await (await mf.dispatchFetch('http://x/')).json();
  await mf.dispose();
  if (decision.status !== 429 || decision.tier !== 'exact') throw new Error(`workerd: ${JSON.stringify(decision)}`);
  console.log('workerd ok');
} finally {
  rmSync(work, { recursive: true, force: true });
}
