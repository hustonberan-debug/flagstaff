/**
 * emulator.mjs — just enough of Cloudflare to test the Worker honestly.
 *
 *  - MemoryKV: get / put (metadata, expirationTtl) / delete / list, with
 *    list() returning at most 1,000 keys and an opaque cursor, as KV does.
 *  - Per-invocation limits, enforced the way the platform does: past the cap,
 *    the next request throws "Too many subrequests." Two modes:
 *      strict  - KV operations count toward the 50 (the cautious reading)
 *      lenient - only fetch() counts toward 50; KV has its own 1,000
 *  - A fake push service standing in for FCM / Mozilla / Apple: records
 *    every delivery, answers 201, or 410 for a "gone" device, or 500 for a
 *    "flaky" one, and can decrypt a payload to prove it is valid.
 */

import { b64urlToBytes, bytesToB64url, concat } from '../webpush.js';

export const LIST_MAX = 1000;
const enc = new TextEncoder();

export class Platform {
  constructor({ mode = 'strict', subrequests = 50, internal = 1000 } = {}) {
    this.mode = mode; this.subrequests = subrequests; this.internal = internal;
    this.current = null;
    this.invocations = [];
  }
  begin() { this.current = { fetch: 0, kv: 0 }; this.invocations.push(this.current); }
  end() { this.current = null; }
  charge(kind) {
    const c = this.current;
    if (!c) return;                         // outside an invocation (test setup)
    // A refused request is not counted, and in lenient mode each kind is
    // refused only by its own limit.
    const over = this.mode === 'strict'
      ? c.fetch + c.kv + 1 > this.subrequests
      : kind === 'fetch' ? c.fetch + 1 > this.subrequests : c.kv + 1 > this.internal;
    if (over) throw new Error('Too many subrequests.');
    if (kind === 'fetch') c.fetch++; else c.kv++;
  }
  get maxPerInvocation() {
    return Math.max(0, ...this.invocations.map((c) =>
      this.mode === 'strict' ? c.fetch + c.kv : c.fetch));
  }
}

export class MemoryKV {
  constructor(platform) { this.platform = platform; this.data = new Map(); this.ops = { get: 0, put: 0, delete: 0, list: 0 }; }
  _live(key) {
    const v = this.data.get(key);
    if (!v) return null;
    if (v.expires && v.expires <= Date.now()) { this.data.delete(key); return null; }
    return v;
  }
  async get(key, type) {
    this.platform.charge('kv'); this.ops.get++;
    const v = this._live(key);
    if (!v) return null;
    return type === 'json' ? JSON.parse(v.value) : v.value;
  }
  async put(key, value, opts = {}) {
    this.platform.charge('kv'); this.ops.put++;
    if (opts.metadata && JSON.stringify(opts.metadata).length > 1024)
      throw new Error('metadata over 1024 bytes');
    this.data.set(key, { value: String(value), metadata: opts.metadata ?? null,
      expires: opts.expirationTtl ? Date.now() + opts.expirationTtl * 1000 : null,
      ttl: opts.expirationTtl ?? null });
  }
  async delete(key) { this.platform.charge('kv'); this.ops.delete++; this.data.delete(key); }
  async list({ prefix = '', cursor, limit = LIST_MAX } = {}) {
    this.platform.charge('kv'); this.ops.list++;
    const names = [...this.data.keys()].filter((k) => k.startsWith(prefix) && this._live(k)).sort();
    const start = cursor ? Number(atob(cursor)) : 0;
    const n = Math.min(limit, LIST_MAX);
    const slice = names.slice(start, start + n);
    const done = start + n >= names.length;
    return { keys: slice.map((name) => ({ name, metadata: this.data.get(name).metadata })),
             list_complete: done, cursor: done ? undefined : btoa(String(start + n)) };
  }
}

// --- Subscribers ------------------------------------------------------------

const ORIGINS = ['https://fcm.googleapis.com/fcm/send', 'https://updates.push.services.mozilla.com/wpush/v2',
                 'https://web.push.apple.com'];

export async function makeClientKeys() {
  const kp = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const pub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  const auth = crypto.getRandomValues(new Uint8Array(16));
  return { privateKey: kp.privateKey, p256dh: bytesToB64url(pub), auth: bytesToB64url(auth) };
}

/**
 * Seed n subscribers straight into KV, as the Worker would have stored them.
 * `legacy` subscribers have no metadata, like every record written before
 * this change. Client key pairs are expensive to make, so one is shared -
 * the push service identifies subscribers by endpoint, not key.
 */
export async function seed(kv, n, { states = () => ['US'], legacy = () => false,
                                    behaviour = () => 'ok', hash } = {}) {
  const keys = await makeClientKeys();
  const subs = [];
  for (let i = 0; i < n; i++) {
    const endpoint = `${ORIGINS[i % ORIGINS.length]}/sub-${i}-${behaviour(i)}`;
    const sub = { endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } };
    const rec = { subscription: sub, states: states(i), updated_at: '2026-09-01T00:00:00Z' };
    const key = 'sub:' + hash(endpoint);
    const prev = kv.platform.current; kv.platform.current = null;
    await kv.put(key, JSON.stringify(rec), legacy(i) ? {} : { metadata: { s: rec.states } });
    kv.platform.current = prev;
    subs.push({ key, endpoint, states: rec.states });
  }
  return { subs, clientKeys: keys };
}

// --- The push service -------------------------------------------------------

export class PushService {
  constructor(platform) {
    this.platform = platform;
    this.deliveries = new Map();          // endpoint -> count
    this.bodies = new Map();              // endpoint -> last body
    this.flakyOnce = new Set();           // endpoints that fail the first time only
    this.seenFlaky = new Set();
    this.audiences = new Set();
  }
  install() {
    this.realFetch = globalThis.fetch;
    globalThis.fetch = async (url, init = {}) => {
      this.platform.charge('fetch');
      const u = String(url);
      const auth = init.headers && init.headers.Authorization || '';
      if (!/^vapid t=[\w-]+\.[\w-]+\.[\w-]+, k=[\w-]+$/.test(auth))
        return new Response('bad auth', { status: 401 });
      if (init.headers['Content-Encoding'] !== 'aes128gcm')
        return new Response('bad encoding', { status: 400 });
      this.audiences.add(new URL(u).origin);
      if (u.endsWith('-gone')) return new Response('', { status: 410 });
      if (u.endsWith('-flaky') && !this.seenFlaky.has(u)) {
        this.seenFlaky.add(u);
        return new Response('try later', { status: 500 });
      }
      this.deliveries.set(u, (this.deliveries.get(u) || 0) + 1);
      this.bodies.set(u, new Uint8Array(init.body));
      return new Response('', { status: 201 });
    };
  }
  uninstall() { globalThis.fetch = this.realFetch; }
  delivered(endpoint) { return this.deliveries.get(endpoint) || 0; }
}

// RFC 8291 decryption, the receiving side, to prove a payload is readable.
export async function decrypt(body, clientKeys) {
  const salt = body.slice(0, 16);
  const idlen = body[20];
  const ephPub = body.slice(21, 21 + idlen);
  const ct = body.slice(21 + idlen);
  const ephKey = await crypto.subtle.importKey('raw', ephPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'ECDH', public: ephKey }, clientKeys.privateKey, 256));
  const hk = async (s, ikm, info, len) => {
    const k = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
    return new Uint8Array(await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: s, info }, k, len * 8));
  };
  const clientPub = b64urlToBytes(clientKeys.p256dh);
  const prk = await hk(b64urlToBytes(clientKeys.auth), shared,
    concat(enc.encode('WebPush: info\0'), clientPub, ephPub), 32);
  const cek = await hk(salt, prk, enc.encode('Content-Encoding: aes128gcm\0'), 16);
  const nonce = await hk(salt, prk, enc.encode('Content-Encoding: nonce\0'), 12);
  const key = await crypto.subtle.importKey('raw', cek, 'AES-GCM', false, ['decrypt']);
  const plain = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: nonce }, key, ct));
  const last = plain.lastIndexOf(2);
  return new TextDecoder().decode(plain.slice(0, last));
}

export async function makeVapid() {
  const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify']);
  const pub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  const jwk = await crypto.subtle.exportKey('jwk', kp.privateKey);
  return { VAPID_PUBLIC_KEY: bytesToB64url(pub), VAPID_PRIVATE_KEY: jwk.d, verifyKey: kp.publicKey };
}

// --- Calling the Worker -----------------------------------------------------

export function makeEnv(kv, vapid, extra = {}) {
  return { SUBS: kv, VAPID_PUBLIC_KEY: vapid.VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY: vapid.VAPID_PRIVATE_KEY,
           VAPID_SUBJECT: 'mailto:test@example.com', NOTIFY_SECRET: 'secret',
           ALLOWED_ORIGIN: 'https://halfstaffnow.com', ...extra };
}

/** One HTTP call = one invocation, with its own subrequest budget. */
export async function call(worker, env, platform, path, body, { auth = true } = {}) {
  const req = new Request('https://worker.test' + path, {
    method: 'POST', body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', ...(auth ? { Authorization: 'Bearer secret' } : {}) },
  });
  platform.begin();
  try {
    const resp = await worker.fetch(req, env);
    return { status: resp.status, body: await resp.json() };
  } finally { platform.end(); }
}
