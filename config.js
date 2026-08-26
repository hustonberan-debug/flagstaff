/**
 * config.js — YOUR settings. This file is yours.
 *
 * It exists because index.html gets regenerated wholesale, and every time it
 * did, the VAPID key and endpoint pasted into it were silently wiped. That
 * turned "here's an updated index.html" into a broken notification system,
 * twice, and cost hours to find because the failure is invisible: the app
 * loads fine, the button works, and pushes just never arrive.
 *
 * So the rule from here on: the file that gets regenerated and the file that
 * holds your keys are never the same file.
 *
 * NEVER put the VAPID *private* key here. This file is public — anyone can
 * read it. The private key lives only in Cloudflare as a secret.
 */

window.FLAGSTAFF_CONFIG = {

  // From `npx web-push generate-vapid-keys` — the PUBLIC one.
  // Also visible at: <your worker>/vapid-public-key
  VAPID_PUBLIC_KEY: 'BLxqFxBsh6CnsC4KfpQHg-0oBDF8s-A0gGz7E8A8T-odCsVtFN0-DZvdL2DUokAHJWH0ErkFwNjbArTejV9i6Ao',

  // Your Cloudflare Worker, with /subscribe on the end.
  SUBSCRIBE_ENDPOINT: 'https://halfstaff-push.hustonberan.workers.dev/subscribe',

  // Shown in the footer so you can tell at a glance which build is live.
  BUILD: '2026-08-26',
};
