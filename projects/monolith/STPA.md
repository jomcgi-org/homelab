# STPA Control Analysis: monolith @ 50f07cb2f

_Auto-generated STPA safety model: the unsafe states this system can reach and the control actions that get it there. Two views: logical (functional control flow) and physical (deployment)._

<details>
<summary><b>How to read this</b>: STPA primer and diagram legend</summary>

**STPA** (System-Theoretic Process Analysis) treats the system as *controllers* issuing *control actions* to *controlled processes*, with *feedback* flowing back up. Instead of "what component can fail," it asks "what control action, given or withheld at the wrong time, drives the system into an unsafe state?" "Unsafe" means a violation of this system's reason to exist, not merely a crash.

Read top-down: **Losses** are outcomes we must never cause; **Hazards** are system states that lead to a loss; the **control-structure diagrams** (one per view) show who commands whom (solid arrows = control actions, dashed = feedback, a node tagged `(designed)` is in the architecture but **not yet built**); the **Unsafe Control Actions** table is the core, and **Unsafe Feedback** covers the dashed arrows: data channels whose absence, staleness, corruption, or spoofing drives a controller into a hazard. Every claim cites `path:line`; unbuilt elements are marked. Semantic, stable IDs mean regenerating changes only the findings that changed.
</details>

**Scope.** A single Postgres-backed personal platform served as two hand-authored binaries: a private read-write binary (all secrets, full app role, private.jomcgi.dev) and an isolated public read-only binary (no secrets, public_reader role, jomcgi.dev via the separate monolith-public chart); safety = governed correctness of data access plus the secret-less public boundary.

<details>
<summary>Maturity detail</summary>

- **Built:** app/main.py private binary, app/main_public.py public binary, Postgres leader-lease singletons (Discord bot, AIS ingest, outbox drain, message-lock sweep), goosecracker agent runner + orphan reclaim, chat feature ACL with 30s grant cache, public_reader/public_writer roles + per-schema grants + view confinement, HTTPRoute public/private split, Turnstile secret isolation, observability snapshot rollup
- **Designed-only:** FastMonolith framework (ADR 010: build_app(profile, modules), per-domain schemas, Module dataclass, api.py cross-domain contract) - no framework/ dir exists yet; per-domain schema isolation
- **Note:** scheduler/api.py still contains the full SKIP-LOCKED dispatch loop but NO binary starts it: app/main.py:203 documents in-process dispatch was removed for Argo CronWorkflows, so the loop is dormant code and only the read/trigger API is live. backend.replicas=1 today, so leader-lease split-brain hazards are latent until HPA scales out.
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
    public-api["Public read surface"]
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
  public-api -- "public.health" --> postgres
  scheduler-api -- "scheduler.trigger" --> postgres
  leader-elector -- "singletons.start" --> bot
  postgres -. "feature grant rows (30s cached)" .-> acl
  postgres -. "recent channel transcript (injected context)" .-> agent-runner
  postgres -. "running_since + runner_instance" .-> agent-runner
  postgres -. "lease heartbeat freshness" .-> leader-elector
  postgres -. "observability topology/stats snapshots" .-> public-api
```

### Physical view

```mermaid
flowchart TD
  subgraph compute["compute"]
    frontend-ssr["SvelteKit SSR sidecar"]
    private-binary["monolith (full secrets)"]
    public-binary["monolith-public (no secrets)"]
  end
  subgraph data["data"]
    migrations["Grant migrations"]
    pg-primary["CNPG primary (-rw)"]
    pg-replica["CNPG hot standby (-ro)"]
  end
  subgraph ingress["ingress"]
    cf-ingress["Cloudflare ingress (Envoy Gateway)"]
  end
  subgraph secrets["secrets"]
    k8s-api["Kubernetes API"]
    onepassword["1Password operator"]
  end
  public-binary -- "chatpublic.write" --> pg-primary
  migrations -- "grant.public-reader" --> pg-replica
  private-binary -- "k8s.mutate" --> k8s-api
  cf-ingress -- "route.public" --> public-binary
  onepassword -- "secret.deliver-public" --> public-binary
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
| `public-tier-write-capability` | physical | The internet-adjacent public tier holds a primary write credential (chat_public) | L.integrity-loss | built |
| `secret-in-wrong-tier` | physical | A private secret or a K8s token is delivered to the public or frontend tier | L.secret-exposure | built |
| `split-brain-singletons` | logical | Two replicas both believe they are leader and run duplicate bot/ingest/drain | L.integrity-loss | built |
| `stale-authz` | logical | A revoked feature grant keeps authorizing an action from the 30s ACL cache | L.unauthorized-access | built |
| `stale-public-snapshot` | logical | The public page serves a stale observability snapshot after the rollup job stops | L.silent-incorrectness | built |
| `wedged-turn` | logical | A queued agent turn never progresses because no leader ever reclaims it | L.liveness-loss | built |

## Control actions

| ID | View | Control action | Controller → Process | Maturity | Evidence |
|----|----|----|----|----|----|
| `acl.check` | logical | Authorize a Discord feature action | `bot` → `acl` | built | projects/monolith/chat/acl.py:68 |
| `agent.reclaim` | logical | Re-dispatch an orphaned agent turn on leader acquire | `leader-elector` → `agent-runner` | built | projects/monolith/chat/goosecracker.py:595 |
| `agent.submit` | logical | Submit an agent turn to the runner | `bot` → `agent-runner` | built | projects/monolith/goosecracker/dispatch.py:60 |
| `chatpublic.write` | physical | Write a chat_public row to the primary as public_writer | `public-binary` → `pg-primary` | built | projects/monolith-public/chart/values.yaml:69 |
| `grant.public-reader` | physical | Grant public_reader SELECT on a schema/view | `migrations` → `pg-replica` | built | projects/monolith/chart/migrations/20260617000000_public_reader_role.sql:24 |
| `k8s.mutate` | physical | Mutate cluster state via private MCP (argocd sync) | `private-binary` → `k8s-api` | built | projects/monolith/app/main.py:244 |
| `leader.acquire` | logical | Acquire/renew/steal the singleton lease | `leader-elector` → `postgres` | built | projects/monolith/app/leadership.py:56 |
| `lock.reclaim` | logical | Reclaim an expired message-processing lock | `bot` → `postgres` | built | projects/monolith/chat/store.py:449 |
| `outbox.post` | logical | Drain and post a queued Discord message/edit/reaction | `outbox` → `bot` | built | projects/monolith/chat/outbox.py:157 |
| `public.health` | logical | Probe read replica as public_reader (SELECT 1) | `public-api` → `postgres` | built | projects/monolith/app/main_public.py:60 |
| `route.public` | physical | Route a hostname path to the public binary | `cf-ingress` → `public-binary` | built | projects/monolith-public/chart/templates/httproute-public.yaml:13 |
| `scheduler.trigger` | logical | Mark a job for immediate run | `scheduler-api` → `postgres` | built | projects/monolith/scheduler/service.py:35 |
| `secret.deliver-public` | physical | Deliver a secret into the public tier | `onepassword` → `public-binary` | built | projects/monolith-public/chart/templates/onepassworditem.yaml:1 |
| `singletons.start` | logical | Start leader-only singletons (bot, ingest, drain, sweep) | `leader-elector` → `bot` | built | projects/monolith/app/main.py:60 |

## Unsafe control actions

*The core of the analysis. Each row: a control action made unsafe via one guideword, the hazard/loss it causes, and where in the code it lives.*

| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `agent.reclaim.providing` | logical | `agent.reclaim` | providing | Reclaim re-dispatches a turn whose old owner is still tearing down or still running it, so the turn executes twice | high | duplicate-agent-run, split-brain-singletons | projects/monolith/chat/goosecracker.py:595 |
| `chatpublic.write.providing` | physical | `chatpublic.write` | providing | The public tier writes a chat_public row without the Turnstile / IP-hash admission gate, poisoning shared snapshots | medium | public-tier-write-capability | projects/monolith-public/chart/values.yaml:69 |
| `grant.public-reader.providing` | physical | `grant.public-reader` | providing | A grant on ALL TABLES or a view lacking the visibility filter exposes private rows to the anonymous tier | high | over-broad-public-grant | projects/monolith/chart/migrations/20260617000000_public_reader_role.sql:24 |
| `k8s.mutate.providing` | physical | `k8s.mutate` | providing | An LLM-driven MCP call issues an argocd sync or resource mutation without an authorization gate | medium | secret-in-wrong-tier | projects/monolith/app/main.py:244 |
| `lock.reclaim.wrong-timing` | logical | `lock.reclaim` | wrong-timing | A slow handler still processing past the 30s TTL is reclaimed and the message is reprocessed concurrently | medium | lock-reclaim-live | projects/monolith/app/main.py:152 |
| `route.public.providing` | physical | `route.public` | providing | A path matching /internal or unfiltered /api/knowledge is added to the public HTTPRoute | high | public-route-exposes-private-path | projects/monolith-public/chart/templates/httproute-public.yaml:13 |
| `secret.deliver-public.providing` | physical | `secret.deliver-public` | providing | The Turnstile secret key or a K8s SA token is wired into the frontend SSR or public tier | high | secret-in-wrong-tier | projects/monolith/public_turnstile_secret_isolation_test.py:76 |

## Unsafe feedback

*Feedback and data channels whose absence, staleness, corruption, or spoofed origin drives a controller into a hazard. This is where data-integrity failures live.*

| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `grants-cache.stale` | logical | `postgres` → `acl`: feature grant rows for guild+subject+scope | stale | A revoked or newly added grant is not seen for up to the 30s cache TTL, so the ACL authorizes on old policy | medium | stale-authz | projects/monolith/chat/acl.py:41 |
| `injected-context.missing` | logical | `postgres` → `agent-runner`: recent channel transcript for the turn | missing | Context assembly fails best-effort and the turn proceeds context-blind, losing the provenance it should have acted on | low | context-blind-turn | projects/monolith/chat/goosecracker.py:865 |
| `lease-heartbeat.stale` | logical | `postgres` → `leader-elector`: leader_lease.heartbeat_at freshness | stale | A leader whose event loop stalls past the 5s TTL (e.g. a sync Session call in async) is stolen from while still holding live singletons, so two replicas run them | high | split-brain-singletons | projects/monolith/app/leadership.py:31 |
| `obs-snapshot.stale` | logical | `postgres` → `public-api`: observability topology/stats snapshot rows | stale | If the Argo rollup CronWorkflow stops, the primed startup snapshot is served indefinitely as if live | medium | stale-public-snapshot | projects/monolith/app/main.py:209 |
| `run-ack.missing` | logical | `postgres` → `agent-runner`: run-accepted acknowledgement from fc-invoke | missing | A read timeout after the run was accepted off-host leaves the runner unable to confirm it ran, so a manual resume can duplicate it | low | duplicate-agent-run | projects/monolith/goosecracker/runner.py:533 |

<details>
<summary><b>Not UCAs</b>: 6 examined and rejected</summary>

- **a single missed leadership heartbeat**: Bounded by 2s renew inside the 5s TTL, and any error resolves to follower fail-safe (projects/monolith/app/leadership.py:30)
- **drain_agent_queue re-own race after handover**: The whole read-check-modify-write runs under with_for_update=True opened at projects/monolith/chat/goosecracker.py:471
- **message-lock reclaim double-claim across pods**: FOR UPDATE SKIP LOCKED rows are held to session.commit(), and the re-GET/update runs in the same transaction (projects/monolith/chat/store.py:474)
- **outbox at-least-once duplicate on crash between post and mark**: Bounded and mostly idempotent: reactions/edits resolve missing targets, a duplicate notify is a rare nuisance (projects/monolith/chat/outbox.py:135)
- **public_reader denied on a private schema**: Not a hazard but the enforced control: DB permission denies the read; asserted by projects/monolith/public_reader_grants_test.py:44
- **scheduler.trigger marks a job that never dispatches**: Inert, not unsafe: no in-process dispatch loop runs; Argo CronWorkflows own execution (projects/monolith/app/main.py:203)
</details>

## Open questions

- FastMonolith (ADR 010) is Draft/designed-only with no framework/ dir; when built, build_app profile validation and per-domain schema grants will materially change the control structure and should be re-analyzed.
- In production, can a chat message handler (LLM summarizer) exceed the 30s message-lock TTL under load and trip lock.reclaim.wrong-timing, or is processing reliably shorter?
- Is the private cluster-mutating MCP surface (k8s-sync-argocd-app, cluster.mcp) authorization-gated for every verb, or does composition into the private binary alone grant it?
- backend.replicas=1 today (deploy/values.yaml:2), so split-brain-singletons and duplicate-agent-run are latent; confirm leader election is exercised once the HPA scales the private tier out.

