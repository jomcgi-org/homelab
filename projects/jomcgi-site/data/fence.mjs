// Release fence (HOME-209). Two paths deploy the site: the Sync-hook Job (a
// release) and the refresh CronJob (new data for the same release). Each
// deploy labels itself with its release (chart version and image digest)
// through `wrangler deploy --message`. A refresh deploys only while the live
// deployment carries its own label; run.sh bounds a refresh's run time and
// makes the hook re-check for longer than that bound, so the hook always
// deploys last. main.mjs runs it as `snapshot fence-check`.

/** The deployment message every deploy of a release carries. */
export const releaseMessage = (release) => `jomcgi-site ${release}`;

/**
 * The live deployment's message for a Worker: null when the Worker has no
 * deployment yet, "" when the latest deployment has no message.
 */
export async function liveMessage(
  { token, account, worker },
  fetchImpl = fetch,
) {
  const response = await fetchImpl(
    `https://api.cloudflare.com/client/v4/accounts/${account}/workers/scripts/${worker}/deployments`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (response.status === 404) return null; // The Worker does not exist yet.
  if (!response.ok) {
    throw new Error(`deployments of ${worker}: ${response.status}`);
  }
  const { result } = await response.json();
  const latest = result?.deployments?.[0];
  if (!latest) return null;
  return latest.annotations?.["workers/message"] ?? "";
}

/**
 * How the live deployment relates to this release: "current" when it carries
 * this release's label, "none" when the Worker has no deployment, and
 * "other" for anything else (another release, or no label).
 */
export function classify(live, release) {
  if (live === null) return "none";
  return live === releaseMessage(release) ? "current" : "other";
}
