/**
 * Tests for nudge.js. Run: npm test
 *
 * The nudge's failure mode is silence: if it dies, the pipeline keeps running
 * on GitHub's fallback cron and everything looks healthy at 3h-stale. So the
 * cases that matter here are the ones where it must NOT quietly do nothing,
 * and the ones where it must NOT dispatch.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { nudge, pipelineBusy, dispatch, NudgeError } from '../nudge.js';

const ENV = { GH_DISPATCH_TOKEN: 'tok', GH_REPO: 'o/r', GH_WORKFLOW: 'check-flags.yml' };

/** A fake GitHub. `busy` is the set of statuses that have runs. */
function gh({ busy = [], dispatchStatus = 204, runsStatus = 200 } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, method: init.method || 'GET', headers: init.headers, body: init.body });
    if (url.includes('/dispatches')) {
      return { ok: dispatchStatus < 300, status: dispatchStatus, text: async () => 'no' };
    }
    const status = new URL(url).searchParams.get('status');
    return { ok: runsStatus === 200, status: runsStatus,
             text: async () => 'boom',
             json: async () => ({ total_count: busy.includes(status) ? 1 : 0 }) };
  };
  return { fetchImpl, calls, dispatched: () => calls.filter(c => c.url.includes('/dispatches')) };
}

function envWithKV() {
  const store = new Map();
  return { env: { ...ENV, SUBS: { put: async (k, v) => void store.set(k, v) } }, store };
}

test('an idle pipeline gets dispatched', async () => {
  const g = gh();
  const out = await nudge(ENV, { fetchImpl: g.fetchImpl });
  assert.equal(out.action, 'dispatched');
  assert.equal(out.ok, true);
  assert.equal(g.dispatched().length, 1);
  assert.equal(JSON.parse(g.dispatched()[0].body).ref, 'main');
});

test('a run already in progress is NOT stacked', async () => {
  const g = gh({ busy: ['in_progress'] });
  const out = await nudge(ENV, { fetchImpl: g.fetchImpl });
  assert.equal(out.action, 'skipped');
  assert.match(out.detail, /in_progress/);
  assert.equal(g.dispatched().length, 0);
});

test('a queued run is NOT stacked', async () => {
  const g = gh({ busy: ['queued'] });
  assert.equal((await nudge(ENV, { fetchImpl: g.fetchImpl })).action, 'skipped');
  assert.equal(g.dispatched().length, 0);
});

test('a run held by the concurrency group is NOT stacked', async () => {
  const g = gh({ busy: ['pending'] });
  assert.equal((await nudge(ENV, { fetchImpl: g.fetchImpl })).action, 'skipped');
  assert.equal(g.dispatched().length, 0);
});

test('a broken runs API does NOT dispatch blind', async () => {
  // Dispatching when we cannot tell whether one is running is how you stack
  // runs during exactly the incident that made the API flaky.
  const g = gh({ runsStatus: 500 });
  const out = await nudge(ENV, { fetchImpl: g.fetchImpl });
  assert.equal(out.ok, false);
  assert.equal(g.dispatched().length, 0);
});

test('a refused dispatch is reported, not swallowed', async () => {
  const g = gh({ dispatchStatus: 403 });          // the PAT expired or lost Actions:write
  const out = await nudge(ENV, { fetchImpl: g.fetchImpl });
  assert.equal(out.ok, false);
  assert.equal(out.action, 'failed');
  assert.match(out.detail, /403/);
});

test('a missing token is reported, not silently skipped', async () => {
  const g = gh();
  const out = await nudge({ GH_REPO: 'o/r' }, { fetchImpl: g.fetchImpl });
  assert.equal(out.ok, false);
  assert.match(out.detail, /GH_DISPATCH_TOKEN/);
  assert.equal(g.dispatched().length, 0);
});

test('nudge never throws - a thrown Cron Trigger is retried, and a retry is a second run', async () => {
  const out = await nudge(ENV, { fetchImpl: async () => { throw new Error('network down'); } });
  assert.equal(out.ok, false);
  assert.match(out.detail, /network down/);
});

test('the outcome is left as a breadcrumb in KV', async () => {
  const { env, store } = envWithKV();
  const g = gh();
  await nudge(env, { fetchImpl: g.fetchImpl, now: () => new Date('2026-09-24T18:00:00Z') });
  const crumb = JSON.parse(store.get('nudge:last'));
  assert.equal(crumb.action, 'dispatched');
  assert.equal(crumb.at, '2026-09-24T18:00:00.000Z');
});

test('a failed breadcrumb does not turn a good dispatch into a failure', async () => {
  const g = gh();
  const env = { ...ENV, SUBS: { put: async () => { throw new Error('KV down'); } } };
  const out = await nudge(env, { fetchImpl: g.fetchImpl });
  assert.equal(out.action, 'dispatched');
  assert.equal(out.ok, true);
});

test('the breadcrumb key cannot be mistaken for a subscriber', async () => {
  // handlers.js lists subscribers with prefix 'sub:'. If the breadcrumb ever
  // gains that prefix it becomes a subscriber record that fails to parse.
  const { env, store } = envWithKV();
  await nudge(env, { fetchImpl: gh().fetchImpl });
  for (const k of store.keys()) assert.ok(!k.startsWith('sub:'), k);
});

test('GitHub is sent a User-Agent, which it requires', async () => {
  const g = gh();
  await nudge(ENV, { fetchImpl: g.fetchImpl });
  for (const c of g.calls) assert.ok(c.headers['User-Agent'], 'missing User-Agent');
});

test('the busy check stays well inside the free plan subrequest budget', async () => {
  const g = gh();
  await nudge(ENV, { fetchImpl: g.fetchImpl });
  assert.ok(g.calls.length <= 10, `${g.calls.length} subrequests`);
});

test('pipelineBusy and dispatch surface NudgeError, not a generic throw', async () => {
  await assert.rejects(() => pipelineBusy({}, gh().fetchImpl), NudgeError);
  await assert.rejects(() => dispatch({}, gh().fetchImpl), NudgeError);
});
