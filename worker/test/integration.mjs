/**
 * integration.mjs — the REAL pipeline script (flagstaff/notify.py) against the
 * REAL Worker, served over HTTP with the free plan's limits enforced per
 * request. Run: npm run test:integration
 *
 * Proves, end to end:
 *   - 200 and 1,500 subscribers are each told exactly once, and no single
 *     Worker invocation goes over 50 subrequests;
 *   - a chunk whose answer is lost to a timeout is retried, and the retry is
 *     answered from the dedup record - nobody is told twice;
 *   - against the ORIGINAL Worker (not yet redeployed), notify.py falls back
 *     to the old call and fails the job, because people were missed.
 *
 * NOTIFY_PY overrides where notify.py is (default: the repo's notify.py).
 */

import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

import worker from '../worker.js';
import { hash } from '../handlers.js';
import { _resetVapidCache } from '../webpush.js';
import { Platform, MemoryKV, PushService, seed, makeVapid, makeEnv } from './emulator.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const NOTIFY_PY = process.env.NOTIFY_PY || path.resolve(here, '../../notify.py');
const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const VAPID = await makeVapid();

function serve(impl, platform, env, { dropFirstChunkAnswerFor = 0 } = {}) {
  let chunkCalls = 0;
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = Buffer.concat(chunks).toString();
    const request = new Request('https://worker.test' + req.url, {
      method: req.method, headers: req.headers, body: req.method === 'POST' ? body : undefined });
    platform.begin();
    let resp;
    try { resp = await impl.fetch(request, env); } finally { platform.end(); }
    const text = await resp.text();
    // The Worker has done the work; now lose the answer, as a timeout would.
    const isChunk = req.url === '/notify' && body.includes('"keys"');
    if (isChunk && chunkCalls++ === 0 && dropFirstChunkAnswerFor) {
      await new Promise((r) => setTimeout(r, dropFirstChunkAnswerFor));
    }
    res.writeHead(resp.status, { 'Content-Type': 'application/json' });
    res.end(text);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

function runNotify(url, statusJson, extraEnv = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'notify-'));
  const file = path.join(dir, 'status.json');
  writeFileSync(file, JSON.stringify(statusJson));
  return new Promise((resolve) => {
    const p = spawn(PYTHON, [NOTIFY_PY, file], {
      env: { ...process.env, NOTIFY_URL: url, NOTIFY_SECRET: 'secret', ...extraEnv } });
    let out = '';
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (out += d));
    p.on('close', (code) => resolve({ code, out }));
  });
}

const national = {
  federal: { reason: 'Patriot Day', observance: 'patriot-day:2026-09-11', _changed: true },
  states: {},
};

async function scenario(name, { n, impl = worker, mode = 'strict', drop = 0, status = national,
                                 expectExit = 0, states }) {
  const platform = new Platform({ mode });
  const kv = new MemoryKV(platform);
  const push = new PushService(platform);
  push.install();
  _resetVapidCache();
  const env = makeEnv(kv, VAPID, { KV_COUNTS_AS_SUBREQUEST: mode === 'strict' ? 'true' : 'false' });
  const { subs } = await seed(kv, n, { hash, ...(states ? { states } : {}) });
  const server = await serve(impl, platform, env, { dropFirstChunkAnswerFor: drop });
  const url = `http://127.0.0.1:${server.address().port}`;
  const started = Date.now();
  const r = await runNotify(url, status, drop ? { NOTIFY_TIMEOUT: '1' } : {});
  server.close();
  push.uninstall();
  const counts = subs.map((s) => push.delivered(s.endpoint));
  const once = counts.filter((c) => c === 1).length;
  const twice = counts.filter((c) => c > 1).length;
  const bySub = subs.map((s, i) => ({ ...s, count: counts[i] }));
  console.log(`${name}: exit ${r.code}, ${once}/${n} told exactly once, ${twice} twice, `
    + `max ${platform.maxPerInvocation} subrequests in one invocation, `
    + `${platform.invocations.length} Worker calls, ${((Date.now() - started) / 1000).toFixed(1)}s`);
  assert.equal(r.code, expectExit, r.out);
  return { once, twice, r, platform, bySub };
}

let failures = 0;
async function check(name, fn) {
  try { await fn(); } catch (e) { failures++; console.error(`FAIL ${name}\n${e.message}`); }
}

await check('200', async () => {
  const s = await scenario('200 subscribers, national', { n: 200 });
  assert.equal(s.once, 200);
  assert.ok(s.platform.maxPerInvocation <= 50);
});
await check('1500', async () => {
  const s = await scenario('1,500 subscribers, national', { n: 1500 });
  assert.equal(s.once, 1500);
  assert.ok(s.platform.maxPerInvocation <= 50);
});
await check('1500 lenient', async () => {
  const s = await scenario('1,500 subscribers, lenient limits', { n: 1500, mode: 'lenient' });
  assert.equal(s.once, 1500);
});
await check('timeout', async () => {
  const s = await scenario('200, first chunk answer lost to a timeout', { n: 200, drop: 2500 });
  assert.equal(s.once, 200);
  assert.equal(s.twice, 0, 'the retry was deduplicated');
  assert.match(s.r.out, /timed out.*retrying/);
});
await check('state', async () => {
  const status = { states: { OH: { effective_status: 'half', changed: true, reason: 'x',
                                   last_changed_at: '2026-09-24T12:00:00Z' } } };
  const s = await scenario('state order, 300 subscribers split OH / NE', { n: 300, status,
    states: (i) => (i % 2 ? ['OH'] : ['NE']) });
  const oh = s.bySub.filter((x) => x.states.includes('OH'));
  const ne = s.bySub.filter((x) => !x.states.includes('OH'));
  assert.ok(oh.every((x) => x.count === 1), 'every Ohio subscriber once');
  assert.ok(ne.every((x) => x.count === 0), 'no Nebraska subscriber');
});
await check('old worker', async () => {
  const original = (await import('../worker.2026-08-15.original.js')).default;
  const realError = console.error; console.error = () => {};
  const s = await scenario('200, Worker NOT yet redeployed', { n: 200, impl: original, mode: 'lenient',
                                                             expectExit: 1 });
  console.error = realError;
  assert.ok(s.once <= 50);
  assert.match(s.r.out, /has not been redeployed/);
  assert.match(s.r.out, /were NOT told/);
});

if (failures) { console.error(`${failures} scenario(s) failed`); process.exit(1); }
console.log('all integration scenarios passed');
