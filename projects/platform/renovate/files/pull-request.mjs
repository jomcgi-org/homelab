// Shared PR handling for bounded maintenance branches. Platform auto-merge is
// default-off until separately activated; this never approves or bypasses checks.
const modes = new Set(["apko", "python", "atlas", "helm", "op", "claude"]);
const mode = process.env.MAINTENANCE_MODE;
if (!modes.has(mode)) throw new Error("Unknown maintenance mode");
const repo = "jomcgi-org/homelab";
const branch = `renovate/${mode}-lock-maintenance`;
const headers = {
  Accept: "application/vnd.github+json",
  Authorization: `Bearer ${process.env.RENOVATE_TOKEN}`,
  "Content-Type": "application/json",
  "X-GitHub-Api-Version": "2022-11-28",
};
async function request(path, body) {
  const response = await fetch(`https://api.github.com/${path}`, {
    headers,
    ...(body === undefined
      ? {}
      : { method: "POST", body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok || result.errors) {
    // Do not print response bodies or headers that could contain credentials.
    throw new Error(`GitHub request failed: ${response.status} ${path}`);
  }
  return result;
}
const pulls = await request(
  `repos/${repo}/pulls?state=open&head=jomcgi-org:${encodeURIComponent(branch)}`,
);
if (!Array.isArray(pulls) || pulls.length > 1)
  throw new Error("Ambiguous branch ownership");
let pull = pulls[0];
if (
  pull &&
  (pull.user.type !== "Bot" ||
    pull.draft ||
    pull.assignees.length ||
    pull.labels.some(({ name }) =>
      /hold|blocked|needs-human|do-not-merge|manual|agent-skip|^human$|^wontfix$|^security-finding$/i.test(
        name,
      ),
    ))
) {
  console.log(`Preserving held or owned PR #${pull.number}`);
  process.exit(3);
}
if (process.argv[2] === "preflight") process.exit(0);
if (process.argv[2] !== "publish") throw new Error("Unknown operation");
pull ||= await request(`repos/${repo}/pulls`, {
  title: `chore(deps): refresh ${mode} locks`,
  head: branch,
  base: "main",
  body: `Scheduled ${mode} dependency maintenance. Requires independent dependency evidence review, Linux pr-checks and the existing merge queue. Generated artifacts and consequential changes remain subject to review.`,
});
await request(`repos/${repo}/issues/${pull.number}/labels`, {
  labels: ["dependencies"],
});
// Old/manual workflows without this explicit flag remain unable to arm a PR,
// even if they load this script from the updated ConfigMap. A false flag does
// not cancel auto-merge or queue membership that another actor already enabled.
if (process.env.MAINTENANCE_AUTOMERGE_ENABLED === "true" && !pull.auto_merge) {
  // Queued PRs have autoMergeRequest:null. Reconcile an existing queue entry
  // before requesting a mutation, and accept immediate queueing as success.
  const observed = await request("graphql", {
    query:
      "query($id: ID!) { node(id: $id) { ... on PullRequest { autoMergeRequest { enabledAt } mergeQueueEntry { id } } } }",
    variables: { id: pull.node_id },
  });
  const state = observed.data?.node;
  if (
    !state ||
    !("autoMergeRequest" in state) ||
    !("mergeQueueEntry" in state)
  ) {
    throw new Error("Current merge state was not confirmed");
  }
  if (!state.autoMergeRequest && !state.mergeQueueEntry) {
    const result = await request("graphql", {
      query:
        "mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: REBASE}) { pullRequest { autoMergeRequest { enabledAt } mergeQueueEntry { id } } } }",
      variables: { id: pull.node_id },
    });
    const armed = result.data?.enablePullRequestAutoMerge?.pullRequest;
    if (!armed?.autoMergeRequest && !armed?.mergeQueueEntry) {
      throw new Error("Auto-merge or queue membership was not confirmed");
    }
  }
}
console.log(`Maintenance PR: ${pull.html_url}`);
