# Verify private factory reads

`POST /api/swarm/factory/synthetics` runs one bounded verification of the
deployed factory status reader. It requires the existing standing human
operator bearer and `factory.synthetics.enabled: true`. The flag defaults off.
There is no scheduler, notification, automatic promotion or rollback.

The reader uses PostgreSQL temporary tables on one dedicated connection. Its
fixture has 128 settled receipts and 1,024 completed starts, all tagged
`synthetic`. ORM schema translation sends the normal `factory_controls.status`
queries to `pg_temp`. The transaction rolls back and the connection closes on
success or error. No row enters the business schemas, intake or public stats.
No model, provider HTTP call, external message or purchase is part of the run.

One warm-up and three measured reads check every receipt, accounting total and
empty active-work bucket. Each read may use at most the measured fixture
baseline of 1,165 SQL reads (9 per receipt plus 13 shared). Fewer reads pass.
The whole run stops before query 6,001 or the next query after 30 seconds;
PostgreSQL bounds each statement to one second and lock waits to 100 ms.
Connection establishment is bounded to two seconds. These are load bounds,
not latency pass/fail thresholds. Unknown cgroup limits or less than 20 percent
headroom refuse the run. An advisory transaction lock serializes runs across
replicas. Calls are manual and not retried automatically.

Two cache scenarios call the real rollout `_cached` function with an explicit
isolated store. Each makes one cold fixture read and three warm reads. Success
and failure must each call their fixture reader once, retain one entry, and
reuse it three times. Cached failures must retain no traceback. No live cache
is cleared, evicted or replaced; no GitHub request is made. A run spanning the
cache TTL is inconclusive rather than a false regression.

The cooldown scenario uses the shipped `GitHubReadCooldown` with its own clock.
Three reads must defer after a simulated 429, then recover at the deadline.
It never changes the live provider cooldown or contacts GitHub.

## Read the evidence

`swarm.synthetic.*` spans use the existing `monolith-backend` OTLP exporter and
collector factory retention policy. Filter Honeycomb on `synthetic.run_id`;
group the status samples by `synthetic.fixture`, `synthetic.chart_version`,
`synthetic.image` and `synthetic.scenario`. Exclude `synthetic.warmup=true`
when comparing durations. Check sample count before comparing runs. Three
samples establish a small baseline; they do not estimate a tail percentile.
Compare the same fixture and application across independently healthy runs.
Keep missing samples or export failures inconclusive, never green.

In the `monolith-backend` dataset, use this query specification, replacing only
`RUN_UUID`. A successful run has three measured samples in each of
`historical-status`, `cache-success` and `cache-failure`. Null fields belong to
other scenarios and do not mean zero. Inspect the `swarm.synthetic.run` and
`swarm.synthetic.cooldown` trace spans for the overall verdict and recovery.

```json
{
  "time_range": 3600,
  "filters": [
    { "column": "synthetic.run_id", "op": "=", "value": "RUN_UUID" },
    { "column": "synthetic.warmup", "op": "=", "value": false }
  ],
  "breakdowns": ["synthetic.scenario", "synthetic.chart_version", "synthetic.image"],
  "calculations": [
    { "op": "COUNT" },
    { "op": "AVG", "column": "synthetic.duration_ms" },
    { "op": "MAX", "column": "synthetic.sql_reads" },
    { "op": "MAX", "column": "synthetic.fixture_reads" },
    { "op": "MAX", "column": "synthetic.cache_entries" }
  ]
}
```

For correctness, inspect `synthetic.correct`, `synthetic.work_within_baseline`
and `synthetic.cache_hit` on those samples. Every measured cache sample must
have `cache_hit=true`, `fixture_reads=0` and `cache_entries=1`. Historical reads
must return `correct=true` and `work_within_baseline=true`.

The server checks requested chart and image against its own deployment env.
The source SHA and Kargo Promotion name are operator-supplied correlation
fields, explicitly labelled as such. Resolve them from the existing chart
publication receipt and Kargo/Argo objects before calling. This adds no
deployment ledger and performs no GitHub ancestry scans.

The response and root span include process/cgroup memory and application cache
counters before and after. Other requests may move those counters. They are
observations, not attributed synthetic cache hits or retained-growth proof.
Per-sample cache hits above are attributable to the isolated application-cache
fixtures. They do not measure real GitHub traffic or the immutable ancestry cache.
It does not restart a pod or test recovery after OOM. A replacement pod or
process instance starts a new comparison window. Keep the deployed memory
diagnostics for longer observations; no resource limit changes are needed.

## Initial production run

Before activation, send this exact scope and the chosen application, chart,
image, source SHA and Promotion to the parent/operator. No live run is implied
by a passing local test.

1. Verify only Application `monolith`: its intended chart, backend image,
   Synced/Healthy state, service response and terminal Kargo Promotion. Check
   the shared-image publication scope before merging or activating the flag.
2. Confirm the existing app role can create temporary tables. Do not grant new
   permissions or add a credential to make the test work. Confirm memory
   headroom and no concurrent rollout. Enable the flag through a reviewed Git
   change only after this scope is accepted.
3. Submit one POST using the existing authorized operator session. Body keys
   are `run_id` (a fresh UUID), `chart_version`, `image` (repository plus digest),
   `source_commit` (full SHA) and `promotion` (Kargo object name). There are no
   user-controlled fixture sizes, SQL, URLs, sample counts or load levels.
4. Stop after this request. Save its result and confirm all fourteen spans arrived
   in Honeycomb before calling the observation complete. A network timeout
   leaves the outcome uncertain; inspect traces and current health before any
   further action. Do not automatically replay the POST.
5. Recheck application health, pod identity and restart count. A local pass
   alone is neither production acceptance nor a rollback authorization.

## Rollout and rollback boundaries

The installed Kargo 1.11.1 chart disables Argo Rollouts integration. Its current
monolith Promotion runs `argocd-update` and `argocd-wait`. Without a separate
verification block, Kargo marks Freight verified after Promotion success;
existing verified Freight therefore proves readiness, not these synthetics.

Kargo's supported [HTTP promotion step](https://docs.kargo.io/user-guide/reference-docs/promotion-steps/http)
can evaluate a JSON result after deployment. It could consume a run-bound
verdict once a supported authenticated path exists. No such credential is
configured for this standing-operator endpoint. Do not expose it anonymously,
put operator tokens in Kargo, or treat an old result as the current promotion.
The API's HTTP 200 can carry `inconclusive`; success must inspect the verdict.

Honeycomb [Query Data permissions](https://docs.honeycomb.io/api/permissions)
require a configuration key with both Manage Queries and Columns and Run
Queries. The collector ingest key is not that key. The repository's trigger
sync manages query definitions, not query results. No connected query API or
account entitlement was verified for this work. If access becomes available,
[query execution is asynchronous](https://docs.honeycomb.io/api/query-data/create-a-query-result)
and a [completed query can still contain an error](https://docs.honeycomb.io/api/query-data/get-query-result).
Missing, incomplete or errored results must remain inconclusive. No new key,
trigger, paid volume commitment or query client is introduced here.

[Kargo automated rollback requires Enterprise](https://akuity.io/blog/kargo-automated-rollbacks).
This repository does not configure it. The existing `kargo_promote` tool can
promote an older eligible version with `rollback=True`; it refuses a concurrent
queued/running Promotion and preserves the Stage's own steps and gates.
The vendored 1.11.1 ProjectConfig CRD confirms `autoRollback` is ignored in OSS.

For a manual recovery, identify the most recent independently verified good
Freight for `kargo-monolith/prod` from Kargo history and retained synthetic
evidence. Require the failed run's exact chart/image still deployed and no
newer Promotion. Inspect every step in the current Stage template: it must
target only Application `monolith`, even if another application shares its
backend image. Check migration/data backward compatibility explicitly. A chart
rollback does not reverse database migrations, shared data, jobs already run,
or external effects. Unknown compatibility or missing good evidence blocks it.

The fast operator path needs no new credential or paid feature:

1. Call `verify_deployment(app="monolith", expected_revision=FAILED_VERSION)`
   and inspect the current revision and Kargo state. If the desired good version
   is already current, verify it and stop. If another version or an active
   Promotion is present, stop and reassess.
2. Call `kargo_promote(app="monolith", chart_version=GOOD_VERSION,
   rollback=True, dry_run=True)`. This checks actual admission, including the
   Kargo webhook, without creating a Promotion. Check the returned namespace,
   Stage and Freight against the reviewed target. Re-read current state before
   the next call; the tool has no atomic expected-current-Promotion condition.
3. Submit the identical call with `dry_run=False` once. Record its returned
   Promotion name. If the create result is lost, inspect Kargo history for the
   result and stop on uncertainty; never blindly replay the call. Keep the
   recovery scoped to this incident and do not recursively roll back a failure.
4. Observe that Promotion and poll `verify_deployment` for at most ten minutes.
   Its revision check accepts the requested version **or newer**, so also
   require the returned/live chart to equal `GOOD_VERSION` exactly. Confirm
   backend image, service response, no competing promotion, and synthetic
   evidence before declaring recovery. Stop on failure or timeout.

Kargo 1.11.1 also has a native per-origin auto-promotion hold. Its vendored Stage
CRD and [Stage documentation](https://docs.kargo.io/user-guide/how-to-guides/working-with-stages)
describe the hold created by a manual promotion of non-candidate Freight. Verify
`status.autoPromotionHolds["Warehouse/monolith-chart"]` names the good Freight
and recovery Promotion, and inspect `status.effectiveAutoPromotionHolds` while
it is in flight. A successful rollback must not be immediately undone by
auto-promotion. Observe the hold and exact revision across the Warehouse's next
five-minute poll. Missing hold or competing intent blocks acceptance; do not
patch Kargo or bypass GitOps to force it. Promoting the selected newest Freight
later can lift the hold, so that is a separate forward-recovery action.

Manual rollback remains available independently of Honeycomb query API access.
Automatic rollback is unarmed; unattended use would additionally need an atomic
current-Promotion condition and once-per-failure handling. No new deployment
ledger, paid upgrade or credentials are needed for the bounded operator path.
