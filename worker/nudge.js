// nudge.js — makes the pipeline actually run on the interval we claim.
//
// WHY THIS EXISTS
//   check-flags.yml asks GitHub for a run every 30 minutes. Measured over the
//   30 days of run history the API retains, GitHub's shared scheduler delivers
//   about 6.5 scheduled runs a day rather than 48: median gap between runs
//   3.2h, p90 about 5.5h, worst observed 12.5h. It is not lag - the start
//   minutes are scattered evenly across the hour instead of drifting from
//   :00/:30, so firings are being dropped, not delayed. A 3h median is too
//   stale for a half-staff alert, so a Cloudflare Cron Trigger calls
//   workflow_dispatch on a schedule that is actually kept.
//
//   GitHub's own cron STAYS in check-flags.yml as the fallback. If this
//   Worker, the token, or Cloudflare's scheduler dies, the pipeline degrades
//   to the ~3h cadence it has today rather than stopping. That is the whole
//   reason not to delete that line once this is deployed.
//
// NOT STACKING RUNS
//   A run takes ~75s, so a 15-minute nudge should never meet one. But the
//   job's own timeout is 25 minutes, so a slow run CAN outlive the interval,
//   and check-flags.yml's concurrency group queues rather than drops. We ask
//   GitHub whether anything is queued or in progress and skip if so.
//
//   This is a check-then-act, so it is not airtight: a run could start in the
//   gap between the check and the dispatch. That is bounded and harmless -
//   the `flag-check` concurrency group serialises whatever slips through, and
//   cache.json is the thing that must not be written twice at once. The check
//   exists to keep a 25-minute run from collecting a queue behind it, not to
//   provide mutual exclusion.
//
// WHEN IT BREAKS
//   A dead nudge is invisible from the outside: the pipeline keeps running on
//   GitHub's fallback cron, status.json keeps being published, and everything
//   looks fine at 3h-stale instead of 15m-stale. So failure is reported in
//   three places, none of which depend on someone watching this Worker:
//     - console.error, visible in `npx wrangler tail` and the dashboard.
//     - a breadcrumb at KV key `nudge:last`, so the last outcome can be read
//       without catching the failure live.
//     - canary.py counts workflow_dispatch runs in the last few hours and
//       fails if there are none. That is the one that actually pages you,
//       and it is deliberately measured from RUNS, not from status.json's
//       timestamp: "nothing has run" and "nothing has changed" are different
//       facts and must not share one value.

const API = 'https://api.github.com';

// Any run in one of these states means the pipeline is already working.
// "waiting" is a run paused on a deployment gate; "pending" is a run held by a
// concurrency group. Both mean: do not add another.
const BUSY = ['queued', 'in_progress', 'waiting', 'pending', 'requested'];

// GitHub rejects API calls with no User-Agent.
const UA = 'halfstaff-nudge (+https://halfstaffnow.com)';

export class NudgeError extends Error {}

function config(env) {
  const token = env.GH_DISPATCH_TOKEN;
  if (!token) throw new NudgeError('GH_DISPATCH_TOKEN is not set');
  return {
    token,
    repo: env.GH_REPO || 'hustonberan-debug/flagstaff',
    workflow: env.GH_WORKFLOW || 'check-flags.yml',
    ref: env.GH_REF || 'main',
  };
}

function headers(token) {
  return {
    'Authorization': `Bearer ${token}`,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': UA,
  };
}

/**
 * Is a run already queued or going? One call per state, each with per_page=1
 * so we read total_count off a small body rather than parsing a page of runs
 * against the free plan's 10ms CPU budget.
 *
 * A failure here is NOT swallowed. Dispatching blind on an API error is how
 * you stack runs during exactly the incident that made the API flaky.
 */
export async function pipelineBusy(env, fetchImpl = fetch) {
  const { token, repo, workflow } = config(env);
  for (const status of BUSY) {
    const url = `${API}/repos/${repo}/actions/workflows/${workflow}/runs`
              + `?status=${status}&per_page=1`;
    const r = await fetchImpl(url, { headers: headers(token) });
    if (!r.ok) {
      throw new NudgeError(`could not read run status (${r.status} ${await safeText(r)})`);
    }
    const body = await r.json();
    if ((body.total_count || 0) > 0) return status;
  }
  return null;
}

/** Ask GitHub to run the workflow now. 204 is the documented success. */
export async function dispatch(env, fetchImpl = fetch) {
  const { token, repo, workflow, ref } = config(env);
  const r = await fetchImpl(
    `${API}/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    { method: 'POST', headers: { ...headers(token), 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref }) });
  if (r.status !== 204) {
    // 401/403 is almost always the PAT: expired, or missing Actions:write.
    // 404 on a repo that exists means the same thing - GitHub hides workflows
    // the token cannot see rather than admitting the permission is missing.
    throw new NudgeError(`dispatch refused (${r.status} ${await safeText(r)})`);
  }
}

async function safeText(r) {
  try { return (await r.text()).slice(0, 200); } catch { return '<no body>'; }
}

/**
 * One scheduled invocation. Never throws: a Cron Trigger that throws is
 * retried by Cloudflare, and a retried dispatch is a second run we just
 * decided not to start. Returns what happened, for tests and for the
 * breadcrumb.
 */
export async function nudge(env, { fetchImpl = fetch, now = () => new Date() } = {}) {
  let outcome;
  try {
    const busy = await pipelineBusy(env, fetchImpl);
    if (busy) {
      outcome = { ok: true, action: 'skipped', detail: `a run is already ${busy}` };
    } else {
      await dispatch(env, fetchImpl);
      outcome = { ok: true, action: 'dispatched' };
    }
  } catch (err) {
    outcome = { ok: false, action: 'failed', detail: (err && err.message) || String(err) };
    console.error('nudge failed:', outcome.detail);
  }
  outcome.at = now().toISOString();

  // Best-effort. The breadcrumb is a convenience; losing it must not turn a
  // successful dispatch into a reported failure.
  try {
    await env.SUBS?.put('nudge:last', JSON.stringify(outcome));
  } catch (err) {
    console.error('nudge breadcrumb not written:', (err && err.message) || String(err));
  }
  return outcome;
}
