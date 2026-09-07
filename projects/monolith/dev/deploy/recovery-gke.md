# Isolated GKE recovery profile

The two `values-recovery-gke.yaml` files under Monolith and EmberVM's
`dev/deploy` directories prepare the selected Monolith replica-loss proof for
the software factory (#4322, recovery implementation #5830). No Application or
kustomization references them. Merging these presets does not create resources
or authorize a fault injection.

Render each preset directly over its published chart defaults, with release
and namespace `monolith-dev` or `embervm-dev`. Do not load production's GKE
values or the historical dev values first: those introduce production restore
sources, external identities, or a disabled DBOS leader lifecycle. Use chart
versions whose packaged source and image pins include the reviewed recovery
implementation and configuration controls. Record the exact versions, source
commits and rendered manifests in the activation packet.

The Monolith profile uses a fresh 10 GiB `standard-rwo` CNPG instance, two
backend replicas, PostgreSQL leader election and the real DBOS drainer. It
permits one manually seeded `qwen-drain` routine per cycle. It creates no Argo
schedule, enables no swarm run lane, and carries no production database
restore, refresh, shared S3 credentials, bot identity or ingress claim. The
`qwen-drain` name is the persisted routine kind; execution uses Luna. Backend
memory stays at the established 1 GiB request/limit per replica. Executor
slimming and concurrency measurement remain separate work in #5861.

The Ember profile provides one 8 GiB brick for a 4 GiB full guest, with other
brick classes and automatic scaling disabled. The small Python task workload
supports the separate fresh-admission check. No second scratch preparation
daemon is installed. Runtime RBAC stays in `embervm-dev`, apart from the
cluster-wide TokenReview permission required to authenticate submitters.
`noded.enabled: false` disables only the legacy DaemonSet. The separate
`bricks.enabled: true` gate renders the class Deployments, each using the
shared noded pod template and its configured guest runtime.

The manually triggered DBOS drainer calls `start_agent_session` directly;
Ember creates and invokes the guest. It does not submit an Argo Workflow.
Keeping `ARGO_JOBS` populated suppresses the inherited scheduler registry
entries while `jobs.image: ""` prevents their CronWorkflows from rendering.

Prepare these facts before creating either deployment:

- Confirm both dev namespaces are unused on the target cluster and no existing
  Application, Kargo stage or refresh job can adopt or reseed them. Stop on an
  ownership conflict. This profile is for fresh state, not conversion of the
  historical dev database.
- Reserve measured node headroom for one additional brick, base building and
  the two backend replicas, including host and production recovery reserves.
  The current production guest count is not a capacity reservation. Confirm
  the selected node has the existing scratch mount; use only its disjoint
  `/var/lib/embervm/scratch/recovery` subtree.
- Provision the planned recovery-only object-store bucket, scoped GCS HMAC
  identity, KEK, noded bearer and registry-read-only pull credential through
  dedicated 1Password items. The registry item supplies `.dockerconfigjson` to
  `embervm-recovery-imagepull-secret`; do not inherit the shared pull item.
  Names in the preset are planned identities, not evidence they exist. Do not
  reuse production credentials or grant the recovery identity access to the
  production base bucket.
- Supply `monolith-dev-pg-embervm-oplog` in both namespaces from one dedicated
  1Password source. CNPG requires the `monolith-dev` copy to have
  `kubernetes.io/basic-auth` type, `username`/`password` keys and its reload
  label. The Ember copy supplies only its op-log connection. Follow the
  existing [op-log credential procedure](../../deploy/embervm-oplog-secret.md)
  with these dev identities; do not execute its production examples unchanged.
  Verify the role/database/host join and reject production connection strings.
- Provision the dedicated Authentik `mcp-recovery` provider and
  `kg-agent-recovery-sa` principal. Join the issuer, audience
  `https://factory-recovery.jomcgi.dev`, broker client ID, app-password source,
  group/scope claims and Monolith verifier. Verify dev tokens work against
  dev and production tokens are rejected by dev. Seed the isolated Codex
  broker grant through its own login; copying production refresh state can
  interfere with rotation and is not part of this plan.
- Prepare GKE-native network policies and verify effective permissions with
  the actual service accounts. Cilium policies are disabled because the hub
  has no Cilium CRDs. Both `noded.networkPolicy` and
  `tokenBroker.networkPolicy` gate `cilium.io/v2` resources. A disabled Cilium
  template is not network isolation. `egress.internal` governs the guest
  proxy, not the control-plane pod's PostgreSQL connection; the native policy
  must separately allow the dev op-log connection to port 5432.
  Allow only the required dev control/progress/MCP/database paths, DNS and
  selected provider/store/authentication egress.
- Build and attest a fresh dev base on the correctly configured brick. The
  guest shim captures `EMBER_PROGRESS_URL` and `EMBER_AGENT_MCP_URL` from
  kernel-delivered environment at base build. `initEnv` changes the identity
  hash but does not supply environment to a restored process. Verify the
  decoded boot arguments, image/base identity, build node and actual callback
  destination before admitting the selected guest.

The activation review must name the exact manifests, credentials' identities
(never their values), provider spend/time limits, admission and fault budget,
selected backend UID, stop conditions and cleanup scope. Root executes and
verifies a prepared operation under the existing delegated Opus/Fable review
policy. This configuration PR does not substitute for that review.
Record the executor node and brick UID too. Preemption, eviction, brick
restart, storage failure or any other competing infrastructure fault
invalidates the selected-backend-loss result. Stop that attempt and preserve
its evidence; do not silently retry or attribute a second fault to Monolith.

For the first recovery proof, admit one routine through the existing drainer
and wait for its real DBOS claim, Ember session/VM identity and persisted
progress. Kill only the elected dev backend. Observe actual leader failover,
DBOS restart and the production recovery hooks, without editing the lease or
calling a recovery function directly. Require one durable unknown-outcome
disposition and retained holds, no redispatch or speculative destroy, preserved
partial evidence and lineage, and rejection of late callbacks. Restart the
recovering backend to check persistence, then prove a separately admitted
fresh task succeeds after observing the first guest release its slot through
normal lifecycle handling. Do not destroy an uncertain guest merely to make
room for the check. Retain unknown cost as unknown.

The existing Ember conformance runner remains the owner of its broader suite
(#5224). Do not build a second runner or make every conformance scenario a
prerequisite for this selected Monolith proof. Automatic conformance and
destructive GC remain disabled in the preset. Cleanup requires exact dev
session, workload, storage and resource identities, preserved evidence, and a
fresh check that production was not affected.
The string sweep gates use `""` to omit their opt-in environment variables;
the control-plane parsers treat absent/empty values as disabled. Rootfs reclaim
receives an empty value and the builder requires exactly `"1"` to delete.
These are the existing chart/runtime contracts, not inheritance from the
production values files.

The existing chart identity targets validate the actual preset files on Linux:
`//projects/monolith/chart:chart_env_identity_test` and
`//projects/embervm/chart:chart_env_identity_test`. Contributor work is combined
on one integration branch before required CI. Passing renders establish
configuration structure; they do not establish live RBAC, database migration,
provider identity, base hydration or recovery behavior.
