# Bosun GitHub App token grants

The token broker can mint different installation tokens from one Bosun GitHub
App. Every grant explicitly selects repository IDs and a permission profile.
Only an authenticated SPIFFE service identity on that grant's allowlist may
retrieve it. The App private key stays in the broker, supplied by the 1Password
Operator. GitHub grants are disabled by default.

This is the token-minting foundation. Factory sessions do not yet receive these
grants, the existing guest GitHub credential path is unchanged, and this change
does not publish or require `factory/review`. Service identity is not proof of
an individual session's role. Do not enable autonomous merge on this foundation
alone.

## Permission profiles

All profiles have metadata, contents and pull requests read access unless the
table grants write access. Checks and commit statuses read access is included
for implementers, reviewers and mergers to inspect validation evidence.

| Profile | Additional permissions | Intended caller |
| --- | --- | --- |
| `planner` | Issues write | Trusted planning/issue service |
| `implementer` | Contents, pull requests and issues write | Trusted implementation gateway |
| `reviewer` | Read only | Trusted review gateway |
| `review-publisher` | Checks write | Trusted verifier of recorded review evidence |
| `merger` | Contents write | Trusted merge executor |

Only `review-publisher` has checks write. An implementer token cannot publish the
required check. However, contents write also permits merging and pull requests
write permits reviews. GitHub does not offer branch-scoped installation tokens,
an issues-comment-only scope, or a create-PR-without-review scope. Restricting
those operations requires a gateway and repository protections. Do not describe
these profiles as complete operation-level capabilities.

Tokens are cached per grant in memory until one minute before GitHub's expiry.
They are not unique per session and are not persisted in Kubernetes Secrets.
Different grants mint independently, even under the same App installation.
Restarting after changing configuration drops the cache, but does not revoke
previously issued tokens: revoke those separately or allow their one-hour
lifetime to expire before declaring a permission reduction complete.

## Register Bosun

Register an organization-owned App under `jomcgi-org`, with the display name
Bosun (use an organization-qualified name if GitHub reports it unavailable).
The name and slug have not been reserved. The required check will bind to the
numeric App ID, not its display name.

[Open the prefilled registration form](https://github.com/organizations/jomcgi-org/settings/apps/new?name=Bosun&description=Homelab%20agent%20factory&url=https%3A%2F%2Fgithub.com%2Fjomcgi-org%2Fhomelab&public=false&webhook_active=false&request_oauth_on_install=false&contents=write&issues=write&pull_requests=write&checks=write&statuses=read).
Review the form before submitting; GitHub can ignore unsupported URL parameters.

Use these settings:

1. Homepage: `https://github.com/jomcgi-org/homelab`.
2. Installation: only this organization, and select only `homelab` initially.
3. Repository permissions: contents, issues, pull requests and checks read/write;
   commit statuses read; metadata read is implicit. No administration, Actions
   write, workflows write, organization permissions, or bypass grants.
4. No user OAuth authorization or callback is needed for installation tokens.
   Leave webhooks inactive until a verified webhook consumer exists.
5. Generate a private key. Store the PEM verbatim in a 1Password item field
   `private-key`. Never put the key, App JWT or installation token in Git,
   a PR, an issue, or an agent prompt.
6. Record the App ID and installation ID separately. Obtain the repository's
   numeric ID with `gh api repos/jomcgi-org/homelab --jq .id` (verified as
   `847803371` during implementation).

The App's permissions are the ceiling. Every mint supplies both `repository_ids`
and `permissions` to `POST /app/installations/{installation_id}/access_tokens`.
Omitting either would inherit broader installation access, so the broker refuses
empty repository lists and unknown profiles. Tokens are opaque strings; no
fixed length or legacy token prefix is assumed.

## Configure the broker

After provisioning SPIFFE identities for the actual trusted callers, set
`tokenBroker.githubApp` in a separate deployment change. The following is a
template, not a deployable overlay: replace the IDs, item path and caller identity
with the registered values. The example authorizes only the publisher service.
It does not create that service or its SPIRE registration.

```yaml
tokenBroker:
  spiffe:
    enabled: true
    clientSpiffeIds:
      # Retain existing trusted broker clients when setting this replacement list.
      - spiffe://embervm.jomcgi.dev/ns/monolith/sa/bosun-review-publisher
  githubApp:
    enabled: true
    appID: "REPLACE_APP_ID"
    installationID: "REPLACE_INSTALLATION_ID"
    onepassword:
      itemPath: "REPLACE_1PASSWORD_ITEM_PATH"
      privateKeyField: private-key
    grants:
      - name: bosun-review-publisher
        profile: review-publisher
        repositoryIDs: [847803371]
        allowedSpiffeIds:
          - spiffe://embervm.jomcgi.dev/ns/monolith/sa/bosun-review-publisher
    clientPodSelectors:
      - matchLabels:
          k8s:io.kubernetes.pod.namespace: monolith
          app.kubernetes.io/component: bosun-review-publisher
```

GitHub grants use `GET /github/grants/{name}/token` on the SPIFFE mTLS listener.
The response has `access_token` and `expires_at`, with `Cache-Control: no-store`.
The endpoint accepts no requested permissions, repository IDs or role headers.
It rejects plaintext callers even if they can reach the broker. The legacy
`/grants/{name}/token` endpoint does not expose GitHub grants. A listener-allowed
SPIFFE identity still needs explicit authorization for the requested grant.

Unlike OAuth grants, GitHub grants need no `embervm-oauth-grant-*` Secret or
Argo ignoreDifferences entry. Never add them to `tokenBroker.grants` or egress
`brokerGrants` pools. The current egress client does not consume this new route.

On Cilium clusters, `clientPodSelectors` permits trusted callers on the TLS port
and GitHub API egress is opened on 443. On the GKE overlay the existing Cilium
policy is disabled, so mTLS remains mandatory. The checked-in SPIFFE listener
default is off; enabling it also requires migrating existing token consumers
off plaintext before their current endpoint starts refusing requests.

## Factory integration and GitHub enforcement

Complete these steps before claiming isolated workload authority:

1. Bind each admitted factory attempt to its server-assigned role, repository,
   task branch/PR, session identity, policy version and lifetime. A model-authored
   DAG, guest header or requested grant name must not assign authority. Unknown
   roles fail closed. Fresh reviewer sessions must not inherit implementation
   credentials, memory or workspace.
2. Propagate a verifiable session binding to the trusted gateway. Use an external
   control-plane record or signed, audience-bound capability, with lifecycle
   checks for stop, restore and expiry. Ember snapshots clone process memory;
   restore-time authority must be refreshed from external state, never derived
   from a copied in-process session token.
3. Map verified roles to broker grants and enforce allowed operations there.
   Do not give the shared noded identity access to publishing or merging grants.
   Keep repository/branch enforcement on Git as well as REST and GraphQL paths.
   Remove the shared `GH_AUTH_TOKEN` fallback for migrated sessions.
4. Add a trusted review publisher. It loads the recorded review from the factory,
   verifies successful completion by an assigned independent review session,
   policy version, PR identity and exact current head SHA, and publishes
   `factory/review`. Set `details_url` to the session evidence and `external_id`
   to the durable review attempt. A session link alone is not proof. Failed,
   cancelled, missing or superseded evidence must not become success.
5. Require `factory/review` in GitHub branch protection or a ruleset, pinned to
   Bosun's numeric App ID, alongside existing CI. First publish a real canary
   check, then configure the required source; never set an "any App" fallback.
   Confirm an unreviewed new head cannot merge, and a later rejecting review of
   the same SHA invalidates the earlier success. Keep all agents off bypass lists.
6. Keep review policy and broker configuration human-owned through CODEOWNERS
   and required code-owner review. One Bosun identity cannot provide native
   self-approval for its own PRs; the required check is the automated gate.
7. Give only the trusted merge executor the merger grant. Validate current CI
   and the published review, then merge with GitHub's expected-head `sha` guard.
   If merge queues are introduced, define and test how the check applies to
   `merge_group` commits before requiring the queue.

The factory's existing independent session and head-SHA evidence in
`projects/monolith/swarm/factory_conductor.py:verify_delivery` is the integration
point. The gate and automatic merge are not implemented by this token provider.

## Validation and rollout

Linux CI must run the provider tests, broker authorization tests and chart
render tests. Local Go/Bazel test runs are prohibited by this repository's agent
instructions. Before enabling, use a canary repository to confirm each token's
permissions, denial of unauthorized callers, and exclusion of repositories not
listed in the grant. Never print issued tokens in test reports.

The implementation investigation could not verify live GKE state: Polylane
returned no accessible workspace, and the local GKE credentials required
reauthentication. Check deployment health and SPIFFE/client readiness before
the activation PR. Disabling the feature stops new broker access but issued
tokens remain valid until expiry or revocation.

References:

- [Installation token scoping and expiry](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [App registration through URL parameters](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-using-url-parameters)
- [GitHub App JWT authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [Check runs and evidence links](https://docs.github.com/en/rest/checks/runs)
- [Required checks and expected App source](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
