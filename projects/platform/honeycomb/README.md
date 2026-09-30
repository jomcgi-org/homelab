# Honeycomb triggers

Honeycomb alert triggers for team `jomcgi-75`, environment `homelab`, kept as
code. Each file in `triggers/` is one trigger. `sync.py` reconciles Honeycomb
with those files through the Triggers and Queries APIs. Every trigger notifies
the Discord webhook recipient `kjEJr2qxxxe` ("Discord homelab alerts").

**The team is on Honeycomb's free plan, which allows exactly one trigger.**
That slot is `jomcgi.dev /health composite unhealthy` (`aJgkA4vC2m8`), the
only file in `triggers/`. Factory alert conditions are evaluated in the
monolith and posted to Discord from there (see
[Alerts that live in the monolith](#alerts-that-live-in-the-monolith)).
`sync.py` enforces this: it refuses to plan when the specs plus any unmanaged
live triggers exceed `PLAN_TRIGGER_LIMIT` (1). Raise it with `--plan-limit`
or `HONEYCOMB_TRIGGER_LIMIT` only after a plan upgrade.

## How the sync behaves

- Triggers are matched to specs by **name**. A spec with no live trigger is
  created; a live trigger that differs from its spec is updated; one that
  matches is left alone. Running it twice changes nothing the second time.
- A live trigger with no spec is printed as `UNMANAGED` and never touched.
  Nothing is ever deleted: remove a trigger in the UI, then delete its file.
- A spec whose dataset does not exist yet is printed as `SKIP`.
- The default is a dry run that prints the plan with a `live:` / `spec:` line
  per drifted field. `--apply` executes it.
- Updates create a new query (Honeycomb queries are immutable) and `PUT` the
  full trigger, so a hand edit in the UI shows up as drift on the next run
  and is overwritten by `--apply`. Change the file instead.

`sync.py` validates the plan limit and the Triggers API limits before calling
anything. The API limits are: one calculation (or one formula), `time_range`
between `frequency` and `min(4 x frequency, 86400)`, no `orders`/`limit`, no
relational fields (`root.`, `parent.`, `any.`), description at most 1023
characters.

## Running it

The sync needs a Honeycomb **configuration key** for the `homelab`
environment in `HONEYCOMB_CONFIG_KEY`. The ingest key the collector uses
(`honeycomb-ingest`) cannot manage triggers.

One-time setup:

1. Honeycomb UI: *Environment settings* for `homelab`, then *API Keys*,
   *Configuration* tab, *Create Configuration API Key*. Name it
   `homelab-trigger-sync`. Grant only **Manage Queries and Columns** and
   **Manage Triggers** (leave Send Events, Manage Recipients, Manage SLOs,
   Manage Boards, Manage Markers and Manage Public Boards off). Copy the key.
2. 1Password: in the `k8s-homelab` vault, create an API Credential item named
   `honeycomb-config-key` with the key in the `credential` field. That is
   `vaults/k8s-homelab/items/honeycomb-config-key`, the same path shape the
   chart `OnePasswordItem`s use, so an in-cluster job can source it later
   without a new item.

Each run, from the repo root:

```bash
export HONEYCOMB_CONFIG_KEY="$(op read 'op://k8s-homelab/honeycomb-config-key/credential')"
python3 projects/platform/honeycomb/sync.py            # dry run
python3 projects/platform/honeycomb/sync.py --apply    # create and update
```

It needs Python 3 and PyYAML, nothing else. Tests:
`//projects/platform/honeycomb:sync_test`.

The first dry run should show one `NOOP` or `UPDATE` for the imported
`jomcgi.dev /health composite unhealthy` trigger. Its `time_range` and
`exceeded_limit` were not visible through the Honeycomb MCP and are taken
from its description. If the dry run shows drift on it, correct the spec file
to the live value before running `--apply`. If it reports an `UNMANAGED`
trigger (for example one of the per-family turn triggers from #6517 that was
applied by hand), the plan refuses: delete that trigger in the Honeycomb UI.

## Triggers

| File | Dataset | Enabled |
| ---- | ------- | ------- |
| `jomcgi-dev-health.yaml` | `metrics` | yes (imported from `aJgkA4vC2m8`) |

## Alerts that live in the monolith

The other alert definitions first written as trigger specs (#6511, #6517)
cannot exist as Honeycomb triggers on this plan. Their specs were deleted and
the conditions moved to monolith health components in
`projects/monolith/factory/ops_health.py`. They are advisory components of the
private `/api/health` (reported under `degraded`, never a 503), and the
monolith leader posts one Discord message when a component flips unhealthy or
recovers, plus a reminder every 6 hours while it stays unhealthy
(`projects/monolith/factory/health_alerts.py`, chart values `healthAlerts`).
Full definitions: [`docs/reference/observability-alerting.md`](../../../docs/reference/observability-alerting.md).

| Former trigger spec | Replaced by |
| ------------------- | ----------- |
| `embervm-session-create-denials.yaml` | `embervm_capacity`: every brick class at 0 replicas, or session creates failing continuously for more than 15 minutes |
| `agent-turns-none-successful-{codex,claude,muse}.yaml` | `agent_turns`: per family (Codex, Claude), at least 2 turns attempted in the last 60 minutes and none delivered. Muse/Spark is not covered |
| `codex-quota-observation-stale.yaml` | `codex_quota_fresh`: broker Codex observation older than 60 minutes while Codex turns were attempted |
| (no spec, #6510) factory tasks stuck uncertain | `factory_stuck`: a receipt uncertain for more than 2 hours, or a queued receipt that is not admission-eligible |
| `egress-proxy-request-denied.yaml` | Not replaced. Needs pod logs; it was disabled and never live |
| `embervm-control-plane-errors.yaml` | Not replaced. Needs pod logs; it was disabled and never live |
| `kargo-promotion-failed.yaml` | Not replaced. Needs pod logs; it was disabled and never live. The advisory `cd` health component reports chart lag |

The span attributes these specs relied on stay useful for investigation in
Honeycomb: `agent.model`, `agent.model_family` and `agent.terminal_reason` on
`agent_sessions.deliver` (#6509) and `ember.placement.outcome` on
`embervm.session.create`.

Additional specs added after this PR opened are removed under the same one-trigger policy: the public probe-absence, demo probe, KG drain, and Loom probe/backup triggers. Their existing telemetry remains available for investigation; this change does not add replacement alerts for those conditions.
