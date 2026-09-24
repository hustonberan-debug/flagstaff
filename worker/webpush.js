/**
 * webpush.js — Web Push for Workers, on the Web Crypto API alone.
 *
 * WHY THIS DOES NOT USE `web-push`
 *   The `web-push` npm package is written for Node: it needs node:crypto,
 *   https.request and Buffer streams. Cloudflare Workers run on V8 isolates,
 *   and `nodejs_compat` does not cover the crypto operations web push
 *   actually performs. Every send threw and landed in `failed`.
 *
 *   So this implements the two specs directly:
 *     - RFC 8292 (VAPID): an ES256-signed JWT proving who is sending.
 *     - RFC 8291 (Message Encryption): ECDH + HKDF + AES-128-GCM so the push
 *       service relays the payload without being able to read it.
 *
 * CPU. The free plan allows 10 ms of CPU per invocation. The VAPID key used to
 * be imported and a JWT signed for EVERY subscriber; both are now cached per
 * push-service origin (the JWT is valid for 12 hours and the same for every
 * subscriber of that service), so a send costs one ECDH + one AES encryption.
 */

// --- base64url helpers ------------------------------------------------------

export function b64urlToBytes(s) {
  s = String(s).replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const raw = atob(s);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function bytesToB64url(bytes) {
  const b = new Uint8Array(bytes);
  let s = '';
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function concat(...arrays) {
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

// Per isolate. Keyed by the key pair, so a rotated secret is picked up.
let vapidKey = null;
let vapidKeyFor = null;
const jwtCache = new Map();          // audience -> { value, exp }
const JWT_LIFETIME_S = 12 * 60 * 60;
const JWT_REFRESH_BEFORE_S = 60 * 60;

export function _resetVapidCache() {
  vapidKey = null; vapidKeyFor = null; jwtCache.clear();
}

export async function vapidHeader(endpoint, env) {
  const aud = new URL(endpoint).origin;
  const now = Math.floor(Date.now() / 1000);
  const id = `${env.VAPID_PUBLIC_KEY}|${env.VAPID_PRIVATE_KEY}`;
  if (vapidKeyFor !== id) {
    vapidKey = importVapidKey(env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);
    vapidKeyFor = id;
    jwtCache.clear();
  }
  const hit = jwtCache.get(aud);
  if (hit && hit.exp - now > JWT_REFRESH_BEFORE_S) return hit.value;

  // The PROMISE is cached, before signing starts. Sends run six at a time,
  // and caching the finished value let all six find the cache empty and sign
  // the same token - 16 signatures for 3 push services in the test.
  const exp = now + JWT_LIFETIME_S;
  const value = signJwt(aud, exp, env);
  jwtCache.set(aud, { value, exp });
  try {
    return await value;
  } catch (err) {
    jwtCache.delete(aud);                 // do not cache a failure
    throw err;
  }
}

async function signJwt(aud, exp, env) {
  const header = bytesToB64url(utf8(JSON.stringify({ typ: 'JWT', alg: 'ES256' })));
  const payload = bytesToB64url(utf8(JSON.stringify({
    aud, exp, sub: env.VAPID_SUBJECT || 'https://halfstaffnow.com',
  })));
  const signingInput = `${header}.${payload}`;
  const sig = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' }, await vapidKey, utf8(signingInput));
  return `vapid t=${signingInput}.${bytesToB64url(sig)}, k=${env.VAPID_PUBLIC_KEY}`;
}

// --- Payload encryption, aes128gcm (RFC 8291) -------------------------------

async function hkdf(salt, ikm, info, length) {
  const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info }, key, length * 8);
  return new Uint8Array(bits);
}

export async function encryptPayload(plaintext, p256dhB64, authB64) {
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

export async function sendPush(subscription, payload, env) {
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
