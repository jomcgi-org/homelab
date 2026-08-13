# STPA Control Analysis: monolith @ 605ec320a

_Auto-generated STPA safety model: the unsafe states this system can reach and the control actions that get it there. Two views: logical (functional control flow) and physical (deployment)._

<details>
<summary><b>How to read this</b>: STPA primer and diagram legend</summary>

**STPA** (System-Theoretic Process Analysis) treats the system as *controllers* issuing *control actions* to *controlled processes*, with *feedback* flowing back up. Instead of "what component can fail," it asks "what control action, given or withheld at the wrong time, drives the system into an unsafe state?" "Unsafe" means a violation of this system's reason to exist, not merely a crash.

Read top-down: **Losses** are outcomes we must never cause; **Hazards** are system states that lead to a loss; the **control-structure diagrams** (one per view) show who commands whom (solid arrows = control actions, dashed = feedback, a node tagged `(designed)` is in the architecture but **not yet built**); the **Unsafe Control Actions** table is the core, and **Unsafe Feedback** covers the dashed arrows: data channels whose absence, staleness, corruption, or spoofing drives a controller into a hazard. Every claim cites `path:line`; unbuilt elements are marked. Semantic, stable IDs mean regenerating changes only the findings that changed.
</details>

**Scope.** A single Postgres-backed personal platform served as two composed binaries: a private read-write binary (full app capabilities, `private.jomcgi.dev`) and an isolated public binary (`jomcgi.dev` via the separate monolith-public chart). The public binary is read-only for the public datasets, but intentionally has two constrained write/invocation paths: `chat_public` through `public_writer` on the primary, and public FaaS invocation through an allow-listed EmberVM identity. The frontend is a separate SSR component. Safety = governed data access, bounded public capabilities, and the secret boundary.

<details>
<summary>Maturity detail</summary>

- **Built:** `framework/core.py` composition and profiles, separate private/public module registries, Postgres leader-lease singletons (Discord bot, AIS ingest, outbox drain, message-lock sweep, agent-session reclaim), goosecracker runner + orphan reclaim, chat feature ACL with 30s grant cache, `public_reader`/`public_writer` roles with schema/view confinement, public/private HTTPRoutes, Turnstile secret isolation, public chat admission/concurrency limits, public FaaS identity gate, and observability snapshot rollup.
- **Designed/convention-only:** strict per-domain database isolation and the ADR 010 cross-domain contract remain architectural goals; the `Module`/`build_app` framework itself is built. The database still has shared cross-domain access in the private binary.
- **Note:** `scheduler/api.py` still contains the full SKIP-LOCKED dispatch loop, but no API lifespan starts it. Active jobs are rendered as Argo CronWorkflows and skipped in-process through `ARGO_JOBS`; the scheduler read/trigger API and dormant legacy loop remain. The private chart enables HPA from 1 to 3 replicas, so leader election is an active horizontal-scaling control rather than merely a future hazard. The public web and frontend also each have HPA up to 3 replicas.
</details>

## Control structure

### Logical view

```mermaid
flowchart TD
  subgraph actor["actor"]
    discord-user["Discord user"]
  end
  subgraph control_plane["control-plane"]
    agent-runner["Goosecracker agent runner"]
    bot["Discord bot (leader singleton)"]
    leader-elector["Leader elector"]
    outbox["Discord outbox drain"]
    scheduler-api["Scheduler read/trigger API"]
  end
  subgraph enforcement["enforcement"]
    acl["Feature ACL (grant cache)"]
  end
  subgraph serving["serving"]
    public-api["Public backend (read + bounded writes)"]
    public-frontend["Public SSR frontend"]
  end
  subgraph store["store"]
    postgres["Postgres (per-domain schemas)"]
  end
  bot -- "acl.check" --> acl
  leader-elector -- "agent.reclaim" --> agent-runner
  bot -- "agent.submit" --> agent-runner
  leader-elector -- "leader.acquire" --> postgres
  bot -- "lock.reclaim" --> postgres
  outbox -- "outbox.post" --> bot
  public-api -- "public.health / chatpublic.write" --> postgres
  scheduler-api -- "scheduler.trigger" --> postgres
  leader-elector -- "singletons.start" --> bot
  postgres -. "feature grant rows (30s cached)" .-> acl
  postgres -. "recent channel transcript (injected context)" .-> agent-runner
  postgres -. "running_since + runner_instance" .-> agent-runner
  postgres -. "lease heartbeat freshness" .-> leader-elector
  postgres -. "observability topology/stats snapshots" .-> public-api
  public-frontend -- "SSR/API proxy" --> public-api
```

### Physical view

```mermaid
flowchart TD
  subgraph compute["compute"]
    frontend-ssr["SvelteKit SSR sidecar"]
    private-binary["monolith (full secrets)"]
    public-binary["monolith-public backend (constrained DB + FaaS access)"]
  end
  subgraph data["data"]
    migrations["Grant migrations"]
    pg-primary["CNPG primary (-rw)"]
    pg-replica["CNPG hot standby (-ro)"]
  end
  subgraph ingress["ingress"]
    cf-ingress["Cloudflare ingress (Envoy Gateway)"]
  end
  subgraph control["control plane"]
    embervm["EmberVM / FaaS admission"]
  end
  subgraph secrets["secrets"]
    k8s-api["Kubernetes API"]
    onepassword["1Password operator"]
  end
  public-binary -- "public_reader reads" --> pg-replica
  public-binary -- "public_writer chat_public writes" --> pg-primary
  public-binary -- "allow-listed FaaS invocation" --> embervm
  private-binary -- "k8s.mutate" --> k8s-api
  cf-ingress -- "route.public" --> frontend-ssr
  frontend-ssr -- "SSR/API proxy" --> public-binary
  onepassword -- "secret.deliver-public" --> public-binary
  onepassword -- "secret.deliver-frontend" --> frontend-ssr
  pg-primary -. "streaming replication lag" .-> pg-replica
```

## Losses

| ID | Loss |
|----|------|
| `L.integrity-loss` | Data is corrupted, or a side-effecting action (Discord post, agent run, chat_public write) is duplicated or forged |
| `L.liveness-loss` | A queued turn or locked message never makes progress |
| `L.provenance-loss` | An agent acts on incomplete or misattributed context, losing lineage of what it was told |
| `L.secret-exposure` | A secret or token reaches a tier or surface that must not hold it |
| `L.silent-incorrectness` | A controller serves stale or wrong data while believing it correct |
| `L.unauthorized-access` | Anonymous or public-tier caller reads data it is not entitled to (private rows, ungoverned paths) |

## Hazards

| ID | View | Hazard (unsafe state) | → Losses | Maturity |
|----|----|----|----|----|
| `context-blind-turn` | logical | An agent turn runs without the injected context it should have had | L.provenance-loss | built |
| `duplicate-agent-run` | logical | An agent turn runs twice (reclaim of a still-live turn, or ambiguous run-accepted ack) | L.integrity-loss | built |
| `lock-reclaim-live` | logical | A message lock is reclaimed while its original processing is still in flight | L.integrity-loss | built |
| `over-broad-public-grant` | physical | public_reader is granted on a schema or view that includes non-public rows | L.unauthorized-access, L.secret-exposure | built |
| `public-route-exposes-private-path` | physical | The public HTTPRoute forwards an internal or unfiltered path to a served handler | L.unauthorized-access | built |
| `public-write-admission-bypass` | physical | The internet-adjacent public tier can write outside the intended `chat_public` path, or bypasses Turnstile, per-session limits, and the cluster-wide inference cap | L.integrity-loss | built control; hazard remains if miswired |
| `secret-in-wrong-tier` | physical | A private secret or a K8s token is delivered to the public or frontend tier | L.secret-exposure | built |
| `split-brain-singletons` | logical | Two replicas both believe they are leader and run duplicate bot/ingest/drain | L.integrity-loss | built |
| `stale-authz` | logical | A revoked feature grant keeps authorizing an action from the 30s ACL cache | L.unauthorized-access | built |
| `stale-public-snapshot` | logical | The public stats endpoint serves an old observability snapshot after the rollup job stops | L.silent-incorrectness | built, bounded by route behavior |
| `wedged-turn` | logical | A queued agent turn never progresses because no leader ever reclaims it | L.liveness-loss | built |

## Control actions

| ID | View | Control action | Controller → Process | Maturity | Evidence |
|----|----|----|----|----|----|
| `acl.check` | logical | Authorize a Discord feature action | `bot` → `acl` | built | projects/monolith/chat/acl.py:68 |
| `agent.reclaim` | logical | Re-dispatch an orphaned agent turn on leader acquire | `leader-elector` → `agent-runner` | built | projects/monolith/chat/goosecracker.py:512 |
| `agent.submit` | logical | Submit an agent turn to the runner | `bot` → `agent-runner` | built | projects/monolith/chat/goosecracker.py:273 |
| `chatpublic.write` | physical | Write a bounded chat_public row to the primary as public_writer | `public-binary` → `pg-primary` | built | projects/monolith-public/chart/values.yaml:82 |
| `grant.public-reader` | physical | Grant public_reader SELECT on a schema/view | `migrations` → `pg-replica` | built | projects/monolith/chart/migrations/20260617000000_public_reader_role.sql:24 |
| `k8s.mutate` | physical | Mutate cluster state via private MCP (ArgoCD sync) | `private-binary` → `k8s-api` | built | projects/monolith/cluster/mcp.py:163 |
| `leader.acquire` | logical | Acquire/renew/steal the singleton lease | `leader-elector` → `postgres` | built | projects/monolith/core/leadership.py:56 |
| `lock.reclaim` | logical | Reclaim an expired message-processing lock | `bot` → `postgres` | built | projects/monolith/chat/leader.py:124 |
| `outbox.post` | logical | Drain and post a queued Discord message/edit/reaction | `outbox` → `bot` | built | projects/monolith/chat/outbox.py:157 |
| `public.health` | logical | Probe the public database as public_reader (SELECT 1) | `public-api` → `postgres` | built | projects/monolith/framework/core.py:282 |
| `route.public` | physical | Route a hostname path to the public binary | `cf-ingress` → `public-binary` | built | projects/monolith-public/chart/templates/httproute-public.yaml:13 |
| `scheduler.trigger` | logical | Mark a job for immediate run | `scheduler-api` → `postgres` | built | projects/monolith/scheduler/service.py:35 |
| `secret.deliver-public` | physical | Deliver only the public backend's constrained DB, Turnstile, and signing material | `onepassword` → `public-binary` | built | projects/monolith-public/chart/templates/onepassworditem.yaml:1 |
| `singletons.start` | logical | Start leader-only singletons (bot, ingest, drain, sweep) | `leader-elector` → `bot` | built | projects/monolith/framework/core.py:213 |

## Unsafe control actions

*The core of the analysis. Each row: a control action made unsafe via one guideword, the hazard/loss it causes, and where in the code it lives.*

| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `agent.reclaim.providing` | logical | `agent.reclaim` | providing | Reclaim re-dispatches a turn whose old owner is still tearing down or still running it, so the turn executes twice | high | duplicate-agent-run, split-brain-singletons | projects/monolith/chat/goosecracker.py:512 |
| `chatpublic.write.providing` | physical | `chatpublic.write` | providing | The public tier accepts a write without the Turnstile/IP-hash admission gate or writes outside the constrained chat_public schema, poisoning or escaping the public boundary | high | public-write-admission-bypass | projects/monolith-public/chart/values.yaml:146 |
| `grant.public-reader.providing` | physical | `grant.public-reader` | providing | A grant on ALL TABLES or a view lacking the visibility filter exposes private rows to the anonymous tier | high | over-broad-public-grant | projects/monolith/chart/migrations/20260617000000_public_reader_role.sql:24 |
| `k8s.mutate.providing` | physical | `k8s.mutate` | providing | An LLM-driven MCP call issues an ArgoCD sync or resource mutation without the private-tier authorization boundary | medium | secret-in-wrong-tier | projects/monolith/cluster/mcp.py:163 |
| `lock.reclaim.wrong-timing` | logical | `lock.reclaim` | wrong-timing | A slow handler still processing past the 30s TTL is reclaimed and the message is reprocessed concurrently | medium | lock-reclaim-live | projects/monolith/chat/leader.py:124 |
| `route.public.providing` | physical | `route.public` | providing | A public HTTPRoute sends an unapproved backend path to the internet, or the frontend SSR proxy exposes a private-only route | high | public-route-exposes-private-path | projects/monolith-public/chart/templates/httproute-public.yaml:20 |
| `secret.deliver-public.providing` | physical | `secret.deliver-public` | providing | A private credential, Kubernetes API-capable token, or Turnstile secret is wired into the public frontend or public backend beyond its explicitly constrained use | high | secret-in-wrong-tier | projects/monolith/public_turnstile_secret_isolation_test.py:76 |

## Unsafe feedback

*Feedback and data channels whose absence, staleness, corruption, or spoofed origin drives a controller into a hazard. This is where data-integrity failures live.*

| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `grants-cache.stale` | logical | `postgres` → `acl`: feature grant rows for guild+subject+scope | stale | A revoked or newly added grant is not seen for up to the 30s cache TTL, so the ACL authorizes on old policy | medium | stale-authz | projects/monolith/chat/acl.py:41 |
| `injected-context.missing` | logical | `postgres` → `agent-runner`: recent channel transcript for the turn | missing | Context assembly fails best-effort and the turn proceeds context-blind, losing the provenance it should have acted on | low | context-blind-turn | projects/monolith/chat/goosecracker.py:865 |
| `lease-heartbeat.stale` | logical | `postgres` → `leader-elector`: leader_lease.heartbeat_at freshness | stale | A leader whose event loop stalls past the 5s TTL (e.g. a sync Session call in async) is stolen from while still holding live singletons, so two replicas run them | high | split-brain-singletons | projects/monolith/core/leadership.py:31 |
| `obs-snapshot.stale` | logical | `postgres` → `public-api`: observability topology/stats snapshot rows | stale | If the Argo rollup stops, the public stats endpoint can serve an old snapshot without an age field or freshness error | medium | stale-public-snapshot | projects/monolith/home/observability/router.py:24 |
| `run-ack.missing` | logical | `postgres` → `agent-runner`: run-accepted acknowledgement from fc-invoke | missing | A read timeout after the run was accepted off-host leaves the runner unable to confirm it ran, so a manual resume can duplicate it | low | duplicate-agent-run | projects/monolith/goosecracker/runner.py:533 |

<details>
<summary><b>Not UCAs</b>: 6 examined and rejected</summary>

- **a single missed leadership heartbeat**: Bounded by 2s renew inside the 5s TTL, and any error resolves to follower fail-safe (projects/monolith/core/leadership.py:29)
- **drain_agent_queue re-own race after handover**: The whole read-check-modify-write runs under with_for_update=True opened at projects/monolith/chat/goosecracker.py:471
- **message-lock reclaim double-claim across pods**: `FOR UPDATE SKIP LOCKED` rows are held to session.commit(), and the reclaim update is transactional (projects/monolith/chat/store.py:701)
- **outbox at-least-once duplicate on crash between post and mark**: Bounded and mostly idempotent: reactions/edits resolve missing targets, a duplicate notify is a rare nuisance (projects/monolith/chat/outbox.py:135)
- **public_reader denied on a private schema**: Not a hazard but the enforced control: DB permission denies the read; asserted by projects/monolith/public_reader_grants_test.py:44
- **scheduler.trigger marks a job that never dispatches**: Inert, not unsafe for currently Argo-owned jobs: `ARGO_JOBS` prevents duplicate in-process registration and active CronWorkflows own execution (projects/monolith/scheduler/api.py:23)
</details>

## Open questions

- The framework is now built, but strict per-domain schema isolation and the cross-domain contract are still conventions. Re-run this analysis when those controls become enforceable rather than merely compositional.
- In production, can a chat message handler (LLM summarizer) exceed the 30s message-lock TTL under load and trip `lock.reclaim.wrong-timing`, or is processing reliably shorter?
- Is the private cluster-mutating MCP surface (`k8s-sync-argocd-app`, `cluster.mcp`) authorization-gated for every verb, or does composition into the private binary alone grant it?
- The private chart enables HPA from 1 to 3 replicas (`projects/monolith/chart/values.yaml:12`); verify leader handover under real scale-out and termination, especially the gap between lease loss and singleton shutdown.
- The public backend has a deliberate primary write path and public FaaS invocation path. Confirm production Cilium/EmberVM policy matches the chart claims: `public_writer` must remain limited to `chat_public`, and the public service account must remain identity-only with no Kubernetes RBAC.
