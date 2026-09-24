/**
 * The Worker's tests. Run: npm test
 *
 * The two scaling caps are proved in both directions: the ORIGINAL Worker
 * (worker.2026-08-15.original.js) is run through the same emulator and shown
 * to stop at 50 and to never look past 1,000, and the new one is shown to
 * reach all 200 and all 1,500 subscribers, exactly once each, with no
 * invocation ever over the platform limit.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker from '../worker.js';
import { hash, Budget, MAX_KEYS_PER_CALL } from '../handlers.js';
import { encryptPayload, vapidHeader, _resetVapidCache, b64urlToBytes } from '../webpush.js';
import { Platform, MemoryKV, PushService, seed, decrypt, makeVapid, makeEnv, call,
         makeClientKeys } from './emulator.mjs';

const VAPID = await makeVapid();

function setup(opts = {}) {
  const platform = new Platform(opts.platform);
  const kv = new MemoryKV(platform);
  const push = new PushService(platform);
  push.install();
  _resetVapidCache();
  const env = makeEnv(kv, VAPID, opts.env);
  return { platform, kv, push, env, done: () => push.uninstall() };
}

/**
 * What the pipeline does (notify.py mirrors this): plan every page, send in
 * chunks, send deferred subscribers in later chunks, retry failures once.
 */
async function fanOut(t, { state = 'US', status = 'half', id = 'n1', chunk = 40 } = {}) {
  const planned = [];
  let next = {};
  do {
    const r = await call(worker, t.env, t.platform, '/notify/plan', { state, ...next });
    assert.equal(r.status, 200, JSON.stringify(r.body));
    planned.push(...r.body.keys);
    next = r.body.next;
  } while (next);
  const queue = [...planned];
  const totals = { sent: 0, gone: 0, missing: 0, skipped: 0, failed: 0, calls: 0, duplicates: 0 };
  const retried = new Set();
  while (queue.length) {
    const keys = queue.splice(0, chunk);
    const r = await call(worker, t.env, t.platform, '/notify',
                         { id, state, status, reason: 'test', keys });
    assert.equal(r.status, 200, JSON.stringify(r.body));
    totals.calls++;
    if (r.body.duplicate) totals.duplicates++;
    for (const k of ['sent', 'gone', 'missing', 'skipped']) totals[k] += r.body[k];
    queue.push(...r.body.deferred);
    for (const k of r.body.failed_keys) {
      if (retried.has(k)) totals.failed++;
      else { retried.add(k); queue.push(k); }
    }
    assert.ok(totals.calls < 10000, 'fan-out did not converge');
  }
  return { planned, ...totals };
}

// --- The caps, before ---------------------------------------------------------

test('BEFORE (lenient limits): the original Worker reaches at most 50 of 200', async () => {
  const t = setup({ platform: { mode: 'lenient' } });
  const original = (await import('../worker.2026-08-15.original.js')).default;
  const { subs } = await seed(t.kv, 200, { hash });
  const r = await call(original, t.env, t.platform, '/notify', { state: 'US', status: 'half' });
  const reached = subs.filter((s) => t.push.delivered(s.endpoint)).length;
  t.done();
  assert.equal(reached, 50, `original reached ${reached} of 200`);
  assert.equal(r.body.failed, 150, 'the other 150 failed');
});

test('BEFORE (strict limits): the original Worker crashes after about 24, telling nobody who was missed', async () => {
  // Its KV read sits outside its try block. When KV counts toward the 50,
  // subscriber ~25's read throws, and the whole call returns a 500 - the
  // rest are not even counted as failed.
  const t = setup({ platform: { mode: 'strict' } });
  const original = (await import('../worker.2026-08-15.original.js')).default;
  const { subs } = await seed(t.kv, 200, { hash });
  const realError = console.error; console.error = () => {};
  const r = await call(original, t.env, t.platform, '/notify', { state: 'US', status: 'half' });
  console.error = realError;
  const reached = subs.filter((s) => t.push.delivered(s.endpoint)).length;
  t.done();
  assert.equal(r.status, 500);
  assert.ok(reached <= 25, `original reached ${reached} of 200`);
});

test('BEFORE: the original Worker never looks past 1,000 subscribers', async () => {
  const t = setup({ platform: { subrequests: 1e9 } });     // lift the 50 to isolate this cap
  const original = (await import('../worker.2026-08-15.original.js')).default;
  const { subs } = await seed(t.kv, 1500, { hash });
  await call(original, t.env, t.platform, '/notify', { state: 'US', status: 'half' });
  const reached = subs.filter((s) => t.push.delivered(s.endpoint)).length;
  t.done();
  assert.equal(reached, 1000, 'subscribers 1,001-1,500 were never considered');
});

// --- The caps, after ------------------------------------------------------------

for (const n of [200, 1500]) {
  for (const mode of ['strict', 'lenient']) {
    test(`AFTER: all ${n} subscribers get a national order exactly once (${mode} limits)`, async () => {
      const t = setup({ platform: { mode },
                        env: { KV_COUNTS_AS_SUBREQUEST: mode === 'strict' ? 'true' : 'false' } });
      const { subs } = await seed(t.kv, n, { hash });
      const r = await fanOut(t);
      const counts = subs.map((s) => t.push.delivered(s.endpoint));
      t.done();
      assert.equal(r.planned.length, n);
      assert.equal(counts.filter((c) => c === 1).length, n, 'every subscriber exactly once');
      assert.ok(t.platform.maxPerInvocation <= 50,
                `an invocation used ${t.platform.maxPerInvocation} subrequests`);
      assert.equal(r.failed, 0);
    });
  }
}

test('AFTER: 1,500 subscribers are planned across two list pages', async () => {
  const t = setup();
  await seed(t.kv, 1500, { hash });
  const r = await fanOut(t);
  t.done();
  assert.equal(r.planned.length, 1500);
  // Two list() calls for planning; none of them read a record, because the
  // states ride in metadata.
  const planCalls = t.platform.invocations.filter((c) => c.fetch === 0).length;
  assert.equal(planCalls, 2);
});

test('the budget adapts: lenient limits need roughly half the chunks', async () => {
  const strict = setup();
  await seed(strict.kv, 400, { hash });
  const a = await fanOut(strict, { chunk: 100 });
  strict.done();
  const lenient = setup({ platform: { mode: 'lenient' }, env: { KV_COUNTS_AS_SUBREQUEST: 'false' } });
  await seed(lenient.kv, 400, { hash });
  const b = await fanOut(lenient, { chunk: 100 });
  lenient.done();
  assert.ok(b.calls < a.calls, `lenient ${b.calls} calls vs strict ${a.calls}`);
});

// --- Who gets it ----------------------------------------------------------------

test("a state order goes only to that state's subscribers", async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 120, { hash, states: (i) => (i % 3 === 0 ? ['OH'] : ['NE', 'IA']) });
  const r = await fanOut(t, { state: 'OH' });
  t.done();
  const oh = subs.filter((s) => s.states.includes('OH'));
  assert.equal(r.planned.length, oh.length);
  assert.ok(oh.every((s) => t.push.delivered(s.endpoint) === 1));
  assert.ok(subs.filter((s) => !s.states.includes('OH')).every((s) => t.push.delivered(s.endpoint) === 0));
});

test('records written before metadata existed are still reached', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 90, { hash, legacy: () => true,
                                          states: (i) => (i % 2 ? ['OH'] : ['NE']) });
  const r = await fanOut(t, { state: 'OH' });
  t.done();
  assert.equal(r.planned.length, 45);
  assert.ok(subs.filter((s) => s.states.includes('OH')).every((s) => t.push.delivered(s.endpoint) === 1));
  assert.ok(t.platform.maxPerInvocation <= 50, 'legacy reads stay inside the budget');
});

test('subscribing writes the states into metadata; dropping a state updates it', async () => {
  const t = setup();
  const ck = await makeClientKeys();
  const sub = { endpoint: 'https://fcm.googleapis.com/fcm/send/abc', keys: { p256dh: ck.p256dh, auth: ck.auth } };
  await call(worker, t.env, t.platform, '/subscribe', { subscription: sub, state: 'OH' }, { auth: false });
  await call(worker, t.env, t.platform, '/subscribe', { subscription: sub, state: 'NE' }, { auth: false });
  const key = 'sub:' + hash(sub.endpoint);
  assert.deepEqual(t.kv.data.get(key).metadata, { s: ['OH', 'NE'] });
  await call(worker, t.env, t.platform, '/drop-state', { endpoint: sub.endpoint, state: 'OH' }, { auth: false });
  assert.deepEqual(t.kv.data.get(key).metadata, { s: ['NE'] });
  const mine = await call(worker, t.env, t.platform, '/my-states', { endpoint: sub.endpoint }, { auth: false });
  t.done();
  assert.deepEqual(mine.body.states, ['NE']);
});

// --- Retries are safe ---------------------------------------------------------------

test('the same chunk sent twice is answered from the record, not sent again', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 10, { hash });
  const body = { id: 'OH:half:2026-09-24', state: 'US', status: 'half', keys: subs.map((s) => s.key) };
  const first = await call(worker, t.env, t.platform, '/notify', body);
  const second = await call(worker, t.env, t.platform, '/notify', body);
  t.done();
  assert.equal(first.body.sent, 10);
  assert.equal(second.body.duplicate, true);
  assert.equal(second.body.sent, 10, 'the stored result is returned');
  assert.ok(subs.every((s) => t.push.delivered(s.endpoint) === 1), 'nobody alerted twice');
  const rec = [...t.kv.data.entries()].find(([k]) => k.startsWith('sent:'))[1];
  assert.equal(rec.ttl, 86400, 'kept for a day');
});

test('a different notification to the same people is not mistaken for a repeat', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 5, { hash });
  const keys = subs.map((s) => s.key);
  await call(worker, t.env, t.platform, '/notify', { id: 'a', state: 'US', status: 'half', keys });
  const r = await call(worker, t.env, t.platform, '/notify', { id: 'b', state: 'US', status: 'full', keys });
  t.done();
  assert.equal(r.body.duplicate, undefined);
  assert.ok(subs.every((s) => t.push.delivered(s.endpoint) === 2));
});

test('a whole fan-out repeated (a retried run) sends nothing the second time', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 150, { hash });
  await fanOut(t, { id: 'US:half:patriot-day:2026-09-11' });
  const again = await fanOut(t, { id: 'US:half:patriot-day:2026-09-11' });
  t.done();
  assert.equal(again.duplicates, again.calls);
  assert.ok(subs.every((s) => t.push.delivered(s.endpoint) === 1));
});

// --- Failures -------------------------------------------------------------------------

test('a device that is gone is removed; a flaky one is retried and reached', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 30, { hash,
    behaviour: (i) => (i === 3 ? 'gone' : i === 7 ? 'flaky' : 'ok') });
  const r = await fanOut(t);
  t.done();
  assert.equal(r.gone, 1);
  assert.equal(t.kv.data.has(subs[3].key), false, 'gone subscription deleted');
  assert.equal(t.push.delivered(subs[7].endpoint), 1, 'flaky subscriber reached on retry');
  assert.equal(r.failed, 0);
});

test('if the platform refuses sooner than the budget assumed, nobody is lost', async () => {
  // The Worker believes it has 50; the platform allows 20.
  const t = setup({ platform: { subrequests: 20 } });
  const { subs } = await seed(t.kv, 100, { hash });
  const r = await fanOut(t);
  t.done();
  assert.ok(subs.every((s) => t.push.delivered(s.endpoint) === 1), 'deferred, then delivered');
  assert.equal(r.failed, 0);
});

test('the notify endpoints require the secret', async () => {
  const t = setup();
  const a = await call(worker, t.env, t.platform, '/notify/plan', { state: 'US' }, { auth: false });
  const b = await call(worker, t.env, t.platform, '/notify', { id: 'x', state: 'US', status: 'half', keys: ['sub:1'] }, { auth: false });
  t.done();
  assert.equal(a.status, 401);
  assert.equal(b.status, 401);
});

test('a chunk rejects bad input rather than guessing', async () => {
  const t = setup();
  const tooMany = Array.from({ length: MAX_KEYS_PER_CALL + 1 }, (_, i) => `sub:${i}`);
  const r1 = await call(worker, t.env, t.platform, '/notify', { id: 'x', state: 'US', status: 'half', keys: tooMany });
  const r2 = await call(worker, t.env, t.platform, '/notify', { state: 'US', status: 'half', keys: ['sub:1'] });
  const r3 = await call(worker, t.env, t.platform, '/notify', { id: 'x', state: 'US', status: 'half', keys: ['evil'] });
  t.done();
  assert.deepEqual([r1.status, r2.status, r3.status], [400, 400, 400]);
});

test('the legacy all-in-one call still works, and reports who it could not reach', async () => {
  const t = setup();
  const { subs } = await seed(t.kv, 60, { hash });
  const r = await call(worker, t.env, t.platform, '/notify', { state: 'US', status: 'half' });
  t.done();
  const reached = subs.filter((s) => t.push.delivered(s.endpoint)).length;
  assert.equal(r.body.sent, reached);
  assert.equal(r.body.deferred_count, 60 - reached, 'the rest are reported, not silently dropped');
  assert.ok(r.body.failed > 0, 'so the pipeline fails the job');
  assert.ok(t.platform.maxPerInvocation <= 50);
});

// --- The crypto ---------------------------------------------------------------------

test('a payload decrypts on the receiving side (RFC 8291)', async () => {
  const ck = await makeClientKeys();
  const body = await encryptPayload('{"state":"OH","status":"half"}', ck.p256dh, ck.auth);
  assert.equal(await decrypt(body, ck), '{"state":"OH","status":"half"}');
});

test('a delivered notification carries the real payload', async () => {
  const t = setup();
  const { subs, clientKeys } = await seed(t.kv, 3, { hash });
  await fanOut(t, { state: 'US', status: 'half' });
  const plain = JSON.parse(await decrypt(t.push.bodies.get(subs[0].endpoint), clientKeys));
  t.done();
  assert.deepEqual([plain.state, plain.status, plain.reason], ['US', 'half', 'test']);
});

test('the VAPID token verifies, and is signed once per push service, not per subscriber', async () => {
  const t = setup();
  const real = crypto.subtle.sign.bind(crypto.subtle);
  let signs = 0;
  crypto.subtle.sign = (...a) => { signs++; return real(...a); };
  try {
    await seed(t.kv, 90, { hash });
    await fanOut(t);
  } finally { crypto.subtle.sign = real; t.done(); }
  assert.equal(signs, t.push.audiences.size, `${signs} signatures for ${t.push.audiences.size} services`);

  _resetVapidCache();
  const header = await vapidHeader('https://fcm.googleapis.com/fcm/send/x', t.env);
  const [, jwt] = header.match(/^vapid t=([^,]+), k=/);
  const [h, p, s] = jwt.split('.');
  const ok = await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, VAPID.verifyKey,
    b64urlToBytes(s), new TextEncoder().encode(`${h}.${p}`));
  const claims = JSON.parse(new TextDecoder().decode(b64urlToBytes(p)));
  assert.equal(ok, true);
  assert.equal(claims.aud, 'https://fcm.googleapis.com');
});

test('budget: strict counts KV, lenient does not', () => {
  const s = new Budget({});
  const l = new Budget({ KV_COUNTS_AS_SUBREQUEST: 'false' });
  assert.equal(s.kv(3), 3);
  assert.equal(l.kv(3), 0);
  assert.equal(s.limit, 46);
});
