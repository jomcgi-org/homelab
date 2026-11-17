// Release fence (HOME-209). Two paths deploy the site: the Sync-hook Job (a
// release) and the refresh CronJob (new data for the same release). Each
// deploy labels itself with its chart version through `wrangler deploy
// --message`. A refresh deploys only while the live deployment carries its
// own label, so a refresh never replaces a different release; the hook
// re-checks after its deploy and deploys again if an in-flight refresh
// replaced it. main.mjs runs it as `snapshot fence-check`.

/** The deployment message every deploy of a chart version carries. */
export const releaseMessage = (chart) => `jomcgi-site chart ${chart}`;

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

/** True when this chart version may deploy over what is live. */
export function isCurrent(live, chart) {
  return live === null || live === releaseMessage(chart);
}
