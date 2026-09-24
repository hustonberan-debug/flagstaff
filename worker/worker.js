/**
 * worker.js — push notification server for halfstaffnow.com
 *
 * Routing only. The endpoints are in handlers.js and the Web Push encryption
 * in webpush.js, so each can be tested on its own (npm test).
 *
 * ENDPOINTS
 *   GET  /vapid-public-key   the key the browser needs to subscribe
 *   POST /subscribe          store a subscription (called by the browser)
 *   POST /unsubscribe        remove one
 *   POST /my-states          the states one subscription watches
 *   POST /drop-state         stop watching one state
 *   POST /notify/plan        which subscribers a notification goes to, one
 *                            page of up to 1,000 at a time (pipeline only)
 *   POST /notify             send to a chunk of subscribers, deduplicated so a
 *                            retry is safe (pipeline only). Without "keys" in
 *                            the body it is the old all-in-one send.
 *
 * Pipeline-only endpoints require NOTIFY_SECRET. See handlers.js for why the
 * fan-out is chunked, and what the budget is.
 */

import {
  json, handleSubscribe, handleUnsubscribe, handleMyStates, handleDropState,
  handleNotify, handleNotifyPlan,
} from './handlers.js';
import { nudge } from './nudge.js';

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
      if (request.method === 'POST') {
        switch (url.pathname) {
          case '/subscribe': return await handleSubscribe(request, env, cors);
          case '/unsubscribe': return await handleUnsubscribe(request, env, cors);
          case '/my-states': return await handleMyStates(request, env, cors);
          case '/drop-state': return await handleDropState(request, env, cors);
          case '/notify/plan': return await handleNotifyPlan(request, env, cors);
          case '/notify': return await handleNotify(request, env, cors);
        }
      }
      return json({ error: 'not found' }, cors, 404);
    } catch (err) {
      console.error('unhandled:', (err && err.stack) || String(err));
      return json({ error: 'server error' }, cors, 500);
    }
  },

  // Cron Trigger (see wrangler.toml). Asks GitHub to run the pipeline, because
  // GitHub's own scheduler delivers the 30-minute cron about every 3 hours.
  // Everything, including why a failure here is not silent, is in nudge.js.
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(nudge(env));
  },
};
