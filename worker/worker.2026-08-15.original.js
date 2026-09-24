/**
 * worker.js — push notification server for halfstaffnow.com
 *
 * WHY THIS DOES NOT USE `web-push`
 *   The `web-push` npm package is written for Node: it needs node:crypto,
 *   https.request and Buffer streams. Cloudflare Workers run on V8 isolates,
 *   and `nodejs_compat` does not cover the crypto operations web push
 *   actually performs. Every send threw and landed in `failed`.
 *
 *   So this implements the two specs directly against the Web Crypto API,
 *   which Workers support natively:
 *     - RFC 8292 (VAPID): an ES256-signed JWT proving who is sending.
 *     - RFC 8291 (Message Encryption): ECDH + HKDF + AES-128-GCM so the push
 *       service relays the payload without being able to read it.
 *
 *   No dependencies at all now.
 *
 * ENDPOINTS
 *   GET  /vapid-public-key   the key the browser needs to subscribe
 *   POST /subscribe          store a subscription (called by the browser)
 *   POST /unsubscribe        remove one
 *   POST /notify             send to everyone watching a state
 *                            (pipeline only — requires NOTIFY_SECRET)
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      'Access-Control-Allow-Origin': env.ALLOWED_ORIGIN || '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    };
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });

    try {
      if (url.pathname === '/vapid-public-key' && request.method === 'GET')
        return json({ key: env.VAPID_PUBLIC_KEY }, cors);
      if (url.pathname === '/subscribe' && request.method === 'POST')
        return await handleSubscribe(request, env, cors);
      if (url.pathname === '/unsubscribe' && request.method === 'POST')
        return await handleUnsubscribe(request, env, cors);
      if (url.pathname === '/my-states' && request.method === 'POST')
        return await handleMyStates(request, env, cors);
      if (url.pathname === '/drop-state' && request.method === 'POST')
        return await handleDropState(request, env, cors);
      if (url.pathname === '/notify' && request.method === 'POST')
        return await handleNotify(request, env, cors);
      return json({ error: 'not found' }, cors, 404);
    } catch (err) {
      console.error('unhandled:', (err && err.stack) || String(err));
      return json({ error: 'server error' }, cors, 500);
    }
  },
};

function json(body, cors, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json', ...cors },
  });
}

// --- base64url helpers ------------------------------------------------------

function b64urlToBytes(s) {
  s = String(s).replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const raw = atob(s);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function bytesToB64url(bytes) {
  const b = new Uint8Array(bytes);
  let s = '';
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function concat(...arrays) {
  const total = arrays.reduce((n, a) => n + a.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrays) { out.set(a, off); off += a.length; }
  return out;
}

const utf8 = (s) => new TextEncoder().encode(s);

// --- VAPID (RFC 8292) -------------------------------------------------------

async function importVapidKey(publicB64, privateB64) {
  const pub = b64urlToBytes(publicB64);     // 65 bytes: 0x04 || X || Y
  const jwk = {
    kty: 'EC', crv: 'P-256', ext: true,
    x: bytesToB64url(pub.slice(1, 33)),
    y: bytesToB64url(pub.slice(33, 65)),
    d: String(privateB64).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  };
  return crypto.subtle.importKey(
    'jwk', jwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
}

async function vapidHeader(endpoint, env) {
  const aud = new URL(endpoint).origin;
  const header = bytesToB64url(utf8(JSON.stringify({ typ: 'JWT', alg: 'ES256' })));
  const payload = bytesToB64url(utf8(JSON.stringify({
    aud,
    exp: Math.floor(Date.now() / 1000) + 12 * 60 * 60,
    sub: env.VAPID_SUBJECT || 'mailto:admin@example.com',
  })));
  const signingInput = `${header}.${payload}`;
  const key = await importVapidKey(env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);
  const sig = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' }, key, utf8(signingInput));
  return `vapid t=${signingInput}.${bytesToB64url(sig)}, k=${env.VAPID_PUBLIC_KEY}`;
}

// --- Payload encryption, aes128gcm (RFC 8291) -------------------------------

async function hkdf(salt, ikm, info, length) {
  const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info }, key, length * 8);
  return new Uint8Array(bits);
}

async function encryptPayload(plaintext, p256dhB64, authB64) {
  const clientPub = b64urlToBytes(p256dhB64);
  const authSecret = b64urlToBytes(authB64);

  const eph = await crypto.subtle.generateKey(
    { name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const ephPubRaw = new Uint8Array(await crypto.subtle.exportKey('raw', eph.publicKey));

  const clientKey = await crypto.subtle.importKey(
    'raw', clientPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'ECDH', public: clientKey }, eph.privateKey, 256));

  const keyInfo = concat(utf8('WebPush: info\0'), clientPub, ephPubRaw);
  const prk = await hkdf(authSecret, shared, keyInfo, 32);

  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, prk, utf8('Content-Encoding: aes128gcm\0'), 16);
  const nonce = await hkdf(salt, prk, utf8('Content-Encoding: nonce\0'), 12);

  // 0x02 is the record delimiter marking the final (only) record.
  const padded = concat(utf8(plaintext), new Uint8Array([2]));
  const aesKey = await crypto.subtle.importKey('raw', cek, 'AES-GCM', false, ['encrypt']);
  const ct = new Uint8Array(await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, tagLength: 128 }, aesKey, padded));

  // header: salt(16) || rs(4) || idlen(1) || ephemeral public key(65)
  const rs = new Uint8Array([0, 0, 16, 0]);          // record size 4096
  const idlen = new Uint8Array([ephPubRaw.length]);
  return concat(salt, rs, idlen, ephPubRaw, ct);
}

async function sendPush(subscription, payload, env) {
  const body = await encryptPayload(
    payload, subscription.keys.p256dh, subscription.keys.auth);
  const auth = await vapidHeader(subscription.endpoint, env);
  return fetch(subscription.endpoint, {
    method: 'POST',
    headers: {
      Authorization: auth,
      'Content-Encoding': 'aes128gcm',
      'Content-Type': 'application/octet-stream',
      TTL: '86400',
      Urgency: 'normal',
    },
    body,
  });
}

// --- Storage ----------------------------------------------------------------

function hash(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(31, h) + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

async function handleSubscribe(request, env, cors) {
  const body = await request.json().catch(() => null);
  const sub = body && body.subscription;
  if (!sub || !sub.endpoint || !sub.keys || !sub.keys.p256dh || !sub.keys.auth)
    return json({ error: 'missing or incomplete subscription' }, cors, 400);

  const state = (body.state || 'US').toUpperCase().slice(0, 2);
  const key = 'sub:' + hash(sub.endpoint);
  const existing = await env.SUBS.get(key, 'json');
  const states = new Set((existing && existing.states) || []);
  states.add(state);

  await env.SUBS.put(key, JSON.stringify({
    subscription: sub, states: [...states], updated_at: new Date().toISOString(),
  }));
  return json({ ok: true, states: [...states] }, cors);
}

async function handleUnsubscribe(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.endpoint) return json({ error: 'missing endpoint' }, cors, 400);
  await env.SUBS.delete('sub:' + hash(body.endpoint));
  return json({ ok: true }, cors);
}

async function handleMyStates(request, env, cors) {
  const body = await request.json().catch(() => null);
  if (!body || !body.endpoint) return json({ error: 'missing endpoint' }, cors, 400);
  const rec = await env.SUBS.get('sub:' + hash(body.endpoint), 'json');
  return json({ states: (rec && rec.states) || [] }, cors);
}

async function handleDropState(request, env, cors) {
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
  await env.SUBS.put(key, JSON.stringify({ ...rec, states,
    updated_at: new Date().toISOString() }));
  return json({ states }, cors);
}

// --- /notify ----------------------------------------------------------------

async function handleNotify(request, env, cors) {
  if ((request.headers.get('Authorization') || '') !== `Bearer ${env.NOTIFY_SECRET}`)
    return json({ error: 'unauthorized' }, cors, 401);

  const body = await request.json().catch(() => null);
  if (!body || !body.state || !body.status)
    return json({ error: 'missing state or status' }, cors, 400);

  const payload = JSON.stringify({
    state: body.state,
    status: body.status,
    reason: body.reason || null,
    url: body.url || 'https://halfstaffnow.com/',
  });

  const target = String(body.state).toUpperCase();
  const list = await env.SUBS.list({ prefix: 'sub:' });

  let sent = 0, gone = 0, failed = 0;
  const errors = [];

  for (const k of list.keys) {
    const rec = await env.SUBS.get(k.name, 'json');
    if (!rec) continue;
    // "US" is a broadcast: a presidential proclamation lowers flags in every
    // state, so it goes to everyone. A governor's order goes ONLY to people
    // watching that state.
    //
    // The first version added "US" to every subscriber's list and then sent
    // to anyone holding it — which meant a Nebraska order notified a
    // subscriber in Florida. Fine with one user, wrong with two.
    if (target !== 'US' && !rec.states.includes(target)) continue;

    try {
      const resp = await sendPush(rec.subscription, payload, env);
      if (resp.status === 404 || resp.status === 410) {
        await env.SUBS.delete(k.name);          // unsubscribed or device gone
        gone++;
      } else if (resp.ok || resp.status === 201) {
        sent++;
      } else {
        failed++;
        const text = await resp.text().catch(() => '');
        errors.push(`${resp.status}: ${text.slice(0, 120)}`);
      }
    } catch (err) {
      failed++;
      errors.push(String((err && err.message) || err).slice(0, 160));
    }
  }

  // Return the actual reason, not just a count. A bare `failed: 2` is exactly
  // the kind of opaque result that costs an afternoon.
  return json({ sent, gone, failed, target, errors: errors.slice(0, 5) }, cors);
}
