/**
 * handlers.js — the Worker's endpoints.
 *
 * SCALING, and why it is shaped like this
 *
 *   The first version sent every notification in ONE invocation: list the
 *   subscribers, then one fetch per subscriber. Two hard caps followed:
 *
 *   1. The Workers Free plan allows 50 subrequests per invocation. Each push
 *      is a subrequest, so on a national proclamation - the most important
 *      day this app has - subscriber 51 onward got nothing.
 *   2. KV list() returns at most 1,000 keys and the cursor was ignored, so
 *      subscriber 1,001 onward was never even considered.
 *
 *   Now the pipeline drives the fan-out in chunks, and every chunk is its own
 *   invocation with its own budget:
 *
 *     POST /notify/plan  -> who should get this? Pages through ALL subscribers
 *                           with the KV cursor. States are kept in each key's
 *                           metadata, so choosing recipients costs one list()
 *                           per 1,000 subscribers rather than a read each.
 *     POST /notify       -> send to these keys. Does as many as fit in this
 *       (with "keys")       invocation's budget and returns the rest as
 *                           `deferred`; the pipeline sends those next.
 *
 *   Chunking from the pipeline was chosen over Queues or a Durable Object
 *   because it needs nothing that is not already deployed - no queue, no
 *   new binding, no second place for a message to get stuck - and the
 *   caller already waits for the answer, so it can see and retry each chunk.
 *
 * THE BUDGET
 *   Cloudflare's docs define a subrequest as "any request a Worker makes using
 *   the Fetch API or to Cloudflare services like R2, KV, or D1", then list a
 *   separate limit of 1,000 "subrequests to internal services". Whether a KV
 *   read counts against the 50 is therefore not certain. The budget assumes
 *   the strict reading (it does count). If that is wrong, the only cost is
 *   more, smaller chunks. Set KV_COUNTS_AS_SUBREQUEST = "false" once you
 *   have confirmed it on your account, and each chunk roughly doubles. And if
 *   the platform refuses a request anyway ("Too many subrequests"), the
 *   subscribers not yet sent are returned as deferred, not failed.
 *
 * RETRIES ARE SAFE
 *   Each chunk's result is stored under a key made from the notification id
 *   and the exact subscribers in it, for a day. The same chunk sent again is
 *   answered from that record without sending anything. The record is written
 *   when the chunk finishes, not when it starts: a claim written first would,
 *   after a crash mid-chunk, make a retry skip people who were never sent to -
 *   a missed alert, which is worse than a duplicate. The one window left is a
 *   retry that arrives while the first attempt is still sending; those people
 *   may be alerted twice.
 */

import { sendPush } from './webpush.js';

export const PLATFORM_SUBREQUEST_LIMIT = 50;
// Held back for anything not counted here: a push service answering with a
// redirect counts as a second subrequest.
export const HEADROOM = 4;
// How many subscriber keys the pipeline may put in one call. More than fit in
// a budget is fine - the rest come back deferred - but a bound keeps the
// request small.
export const MAX_KEYS_PER_CALL = 100;
export const DEDUP_TTL_S = 24 * 60 * 60;
// Browsers allow 6 connections waiting for headers per invocation; sending in
// groups of 6 keeps them all busy without queueing inside the runtime.
const SEND_CONCURRENCY = 6;
const LIST_PAGE = 1000;

export function json(body, cors, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json', ...cors },
  });
}

// --- Storage ----------------------------------------------------------------

export function hash(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(31, h) + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

// The states a subscriber watches ride in the key's metadata, so /notify/plan
// can choose recipients from list() alone. Written on every subscribe and
// drop; records from before this change have none and are read instead.
function saveRecord(env, key, rec) {
  return env.SUBS.put(key, JSON.stringify(rec), { metadata: { s: rec.states } });
}

export async function handleSubscribe(request, env, cors) {
  const body = await request.json().catch(() => null);
  const sub = body && body.subscription;
  if (!sub || !sub.endpoint || !sub.keys || !sub.keys.p256dh || !sub.keys.auth)
    return json({ error: 'missing or incomplete subscription' }, cors, 400);

  const state = (body.state || 'US').toUpperCase().slice(0, 2);
  const key = 'sub:' + hash(sub.endpoint);
  const existing = await env.SUBS.get(key, 'json');
  const states = new Set((existing && existing.states) || []);
  states.add(state);

  await saveRecord(env, key, {
    subscription: sub, states: [...states], updated_at: new Date().toISOString(),
  });
  return json({ ok: true, states: [...states] }, cors);
}

export async function handleUnsubscribe(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.endpoint) return json({ error: 'missing endpoint' }, cors, 400);
  await env.SUBS.delete('sub:' + hash(body.endpoint));
  return json({ ok: true }, cors);
}

export async function handleMyStates(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.endpoint) return json({ error: 'missing endpoint' }, cors, 400);
  const rec = await env.SUBS.get('sub:' + hash(body.endpoint), 'json');
  return json({ states: (rec && rec.states) || [] }, cors);
}

export async function handleDropState(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.endpoint || !body.state)
    return json({ error: 'missing endpoint or state' }, cors, 400);

  const key = 'sub:' + hash(body.endpoint);
  const rec = await env.SUBS.get(key, 'json');
  if (!rec) return json({ states: [] }, cors);

  const states = (rec.states || []).filter(
    (s) => s !== String(body.state).toUpperCase());

  // Watching nothing means the record has no purpose; drop it entirely
  // rather than leaving an empty subscription to be pushed to forever.
  if (states.length === 0) {
    await env.SUBS.delete(key);
    return json({ states: [] }, cors);
  }
  await saveRecord(env, key, { ...rec, states, updated_at: new Date().toISOString() });
  return json({ states }, cors);
}

// --- The budget -------------------------------------------------------------

export class Budget {
  constructor(env) {
    const limit = Number(env.SUBREQUEST_LIMIT) || PLATFORM_SUBREQUEST_LIMIT;
    this.limit = limit - HEADROOM;
    this.kvCounts = String(env.KV_COUNTS_AS_SUBREQUEST ?? 'true').toLowerCase() !== 'false';
    this.used = 0;
  }
  kv(n = 1) { return this.kvCounts ? n : 0; }
  fits(n) { return this.used + n <= this.limit; }
  spend(n) { this.used += n; }
}

const isSubrequestLimit = (err) => /too many subrequests/i.test(String(err && err.message || err));

function authorized(request, env) {
  return (request.headers.get('Authorization') || '') === `Bearer ${env.NOTIFY_SECRET}`;
}

function wants(states, target) {
  // "US" is a broadcast: a presidential proclamation lowers flags in every
  // state, so it goes to everyone. A governor's order goes ONLY to people
  // watching that state. (The first version added "US" to every list and
  // sent to anyone holding it - a Nebraska order notified Florida.)
  return target === 'US' || (Array.isArray(states) && states.includes(target));
}

// --- /notify/plan -----------------------------------------------------------

/**
 * Body: { state, cursor?, offset? }
 * Returns { keys: [...], next: {cursor, offset} | null, scanned, legacy_reads }.
 * Call again with `next` until it is null.
 */
export async function handleNotifyPlan(request, env, cors) {
  if (!authorized(request, env)) return json({ error: 'unauthorized' }, cors, 401);
  const body = await request.json().catch(() => null);
  if (!body || !body.state) return json({ error: 'missing state' }, cors, 400);
  const target = String(body.state).toUpperCase();
  const budget = new Budget(env);

  budget.spend(budget.kv(1));
  const page = await env.SUBS.list({ prefix: 'sub:', cursor: body.cursor || undefined,
                                     limit: LIST_PAGE });
  const keys = [];
  let legacy = 0;
  const start = Math.max(0, Number(body.offset) || 0);
  for (let i = start; i < page.keys.length; i++) {
    const k = page.keys[i];
    let states = k.metadata && k.metadata.s;
    if (!Array.isArray(states)) {
      // A record from before states were kept in metadata: read it. Stop
      // before the budget runs out and hand back where to resume.
      if (!budget.fits(budget.kv(1))) {
        return json({ keys, next: { cursor: body.cursor || null, offset: i },
                      scanned: i - start, legacy_reads: legacy }, cors);
      }
      budget.spend(budget.kv(1));
      const rec = await env.SUBS.get(k.name, 'json');
      legacy++;
      states = rec && rec.states;
      if (!rec) continue;
    }
    if (wants(states, target)) keys.push(k.name);
  }
  const next = page.list_complete ? null : { cursor: page.cursor, offset: 0 };
  return json({ keys, next, scanned: page.keys.length - start, legacy_reads: legacy }, cors);
}

// --- /notify (a chunk) ------------------------------------------------------

async function sha256hex(s) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Body: { id, state, status, reason?, url?, keys: [subscriber keys] }
 * Returns { sent, gone, missing, skipped, failed, failed_keys, deferred,
 *           errors, duplicate? }.
 */
export async function handleNotifyChunk(body, env, cors) {
  const keys = body.keys;
  if (!body.id || typeof body.id !== 'string' || body.id.length > 200)
    return json({ error: 'missing or bad id' }, cors, 400);
  if (!Array.isArray(keys) || keys.length === 0 || keys.length > MAX_KEYS_PER_CALL
      || !keys.every((k) => typeof k === 'string' && k.startsWith('sub:')))
    return json({ error: `keys must be 1-${MAX_KEYS_PER_CALL} subscriber keys` }, cors, 400);

  const target = String(body.state).toUpperCase();
  const budget = new Budget(env);
  const dedupKey = 'sent:' + await sha256hex(`${body.id}|${[...keys].sort().join(',')}`);

  budget.spend(budget.kv(1));
  const prior = await env.SUBS.get(dedupKey, 'json');
  if (prior) return json({ ...prior, duplicate: true }, cors);
  budget.spend(budget.kv(1));             // reserved: the record written at the end

  const payload = JSON.stringify({
    state: body.state, status: body.status,
    reason: body.reason || null, url: body.url || 'https://halfstaffnow.com/',
  });

  const out = { id: body.id, sent: 0, gone: 0, missing: 0, skipped: 0,
                failed_keys: [], deferred: [], errors: [] };
  // Worst case for one subscriber: read it, push to it, delete it if gone.
  const perSub = budget.kv(1) + 1 + budget.kv(1);
  let i = 0;
  while (i < keys.length) {
    const room = Math.floor((budget.limit - budget.used) / perSub);
    if (room <= 0) break;                              // the rest are deferred below
    const group = keys.slice(i, i + Math.min(SEND_CONCURRENCY, room));
    budget.spend(perSub * group.length);
    const results = await Promise.all(group.map((k) => sendOne(k, target, payload, env)));
    let refused = false;
    results.forEach((r, j) => {
      if (r.kind === 'deferred') { out.deferred.push(group[j]); refused = true; }
      else if (r.kind === 'failed') {
        out.failed_keys.push(group[j]);
        if (r.error) out.errors.push(r.error);
      } else out[r.kind]++;
    });
    i += group.length;
    // The platform refused a request: nothing after it will get through.
    if (refused) break;
  }
  out.deferred.push(...keys.slice(i));

  out.failed = out.failed_keys.length;
  out.errors = out.errors.slice(0, 5);
  out.used = budget.used;
  try {
    await env.SUBS.put(dedupKey, JSON.stringify(out), { expirationTtl: DEDUP_TTL_S });
  } catch (err) {
    // Not fatal: the sends happened. A retry of this exact chunk would repeat
    // them, which is the lesser harm.
    out.dedup_saved = false;
  }
  return json(out, cors);
}

async function sendOne(key, target, payload, env) {
  let rec;
  try {
    rec = await env.SUBS.get(key, 'json');
  } catch (err) {
    return isSubrequestLimit(err) ? { kind: 'deferred' }
      : { kind: 'failed', error: `read ${key}: ${String(err && err.message || err).slice(0, 100)}` };
  }
  if (!rec || !rec.subscription) return { kind: 'missing' };
  // The plan can be a few seconds stale: someone may have dropped this state.
  if (!wants(rec.states, target)) return { kind: 'skipped' };
  try {
    const resp = await sendPush(rec.subscription, payload, env);
    if (resp.status === 404 || resp.status === 410) {
      await env.SUBS.delete(key).catch(() => {});   // unsubscribed or device gone
      return { kind: 'gone' };
    }
    if (resp.ok) return { kind: 'sent' };
    const text = await resp.text().catch(() => '');
    return { kind: 'failed', error: `${resp.status}: ${text.slice(0, 120)}` };
  } catch (err) {
    if (isSubrequestLimit(err)) return { kind: 'deferred' };
    return { kind: 'failed', error: String((err && err.message) || err).slice(0, 160) };
  }
}

// --- /notify (legacy, single call) -------------------------------------------

/**
 * The original all-in-one send, kept so the pipeline keeps working in either
 * deployment order. It still stops at the budget: whoever does not fit is
 * reported in `deferred_count`, and counted as failed, rather than silently
 * dropped as before. The pipeline only uses this against a Worker that does
 * not have /notify/plan.
 */
export async function handleNotifyLegacy(body, env, cors) {
  const target = String(body.state).toUpperCase();
  const payload = JSON.stringify({
    state: body.state, status: body.status,
    reason: body.reason || null, url: body.url || 'https://halfstaffnow.com/',
  });
  const budget = new Budget(env);
  const perSub = budget.kv(1) + 1 + budget.kv(1);
  let sent = 0, gone = 0, failed = 0, deferred = 0;
  const errors = [];
  let cursor;
  do {
    budget.spend(budget.kv(1));
    const page = await env.SUBS.list({ prefix: 'sub:', cursor, limit: LIST_PAGE });
    for (const k of page.keys) {
      const meta = k.metadata && k.metadata.s;
      if (Array.isArray(meta) && !wants(meta, target)) continue;
      if (!budget.fits(perSub)) { deferred++; continue; }
      budget.spend(perSub);
      const r = await sendOne(k.name, target, payload, env);
      if (r.kind === 'sent') sent++;
      else if (r.kind === 'gone') gone++;
      else if (r.kind === 'deferred') deferred++;
      else if (r.kind === 'failed') { failed++; if (r.error) errors.push(r.error); }
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor && budget.fits(budget.kv(1)));
  if (cursor) deferred++;                // at least one page never listed
  return json({ sent, gone, failed: failed + deferred, deferred_count: deferred,
                target, errors: errors.slice(0, 5) }, cors);
}

export async function handleNotify(request, env, cors) {
  if (!authorized(request, env)) return json({ error: 'unauthorized' }, cors, 401);
  const body = await request.json().catch(() => null);
  if (!body || !body.state || !body.status)
    return json({ error: 'missing state or status' }, cors, 400);
  return body.keys ? handleNotifyChunk(body, env, cors) : handleNotifyLegacy(body, env, cors);
}
