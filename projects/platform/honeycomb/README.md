# Honeycomb triggers

Honeycomb alert triggers for team `jomcgi-75`, environment `homelab`, kept as
code. Each file in `triggers/` is one trigger. `sync.py` reconciles Honeycomb
with those files through the Triggers and Queries APIs. Every trigger notifies
the Discord webhook recipient `kjEJr2qxxxe` ("Discord homelab alerts").

## How the sync behaves

- Triggers are matched to specs by **name**. A spec with no live trigger is
  created; a live trigger that differs from its spec is updated; one that
  matches is left alone. Running it twice changes nothing the second time.
- A live trigger with no spec is printed as `UNMANAGED` and never touched.
  Nothing is ever deleted: remove a trigger in the UI, then delete its file.
- A spec whose dataset does not exist yet is printed as `SKIP`. The log-based
  triggers stay in that state until pod-log shipping creates `k8s-logs`.
- The default is a dry run that prints the plan with a `live:` / `spec:` line
  per drifted field. `--apply` executes it.
- Updates create a new query (Honeycomb queries are immutable) and `PUT` the
  full trigger, so a hand edit in the UI shows up as drift on the next run
  and is overwritten by `--apply`. Change the file instead.

`sync.py` validates the Triggers API limits before calling anything: one
calculation (or one formula), `time_range` between `frequency` and
`min(4 x frequency, 86400)`, no `orders`/`limit`, no relational fields
(`root.`, `parent.`, `any.`), description at most 1023 characters.

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
from its description. Its probe is not running on the hub, so it currently
sees no data (#6507). If the dry run shows drift on it, correct the spec file
to the live value before running `--apply`.

## Triggers

| File | Dataset | Enabled |
| ---- | ------- | ------- |
| `jomcgi-dev-health.yaml` | `metrics` | yes (imported from `aJgkA4vC2m8`) |
| `embervm-session-create-denials.yaml` | `embervm-control` | yes |
| `agent-turns-none-successful.yaml` | `monolith-backend` | yes |
| `codex-quota-observation-stale.yaml` | `monolith-backend` | no, see below |
| `egress-proxy-request-denied.yaml` | `k8s-logs` | no, logs not shipped yet |
| `embervm-control-plane-errors.yaml` | `k8s-logs` | no, logs not shipped yet |
| `kargo-promotion-failed.yaml` | `k8s-logs` | no, logs not shipped yet |

Each file's `description` says what the query measures and where its
threshold came from. The thresholds were set from the 7 days of data to
2026-09-30.

To enable the log triggers: once the collector ships pod logs, confirm the
dataset slug and the `body`, `k8s.namespace.name` and `k8s.container.name`
columns against real events, fix the files if they differ, set
`enabled: true`, and run the sync.

## Signals that are not queryable yet

These need instrumentation before they can alert.

- **Codex quota age.** `drain.quota.codex.age_seconds` is set only on
  `drain.job` spans (28 in the 7 days to 2026-09-30). An idle or wedged
  drainer writes nothing, so a staleness trigger cannot fire when it should.
  Set the same attribute on every `drain.cycle` span (about every 16 minutes)
  in `projects/monolith/factory/orchestration/drainer.py`, then enable
  `codex-quota-observation-stale.yaml` unchanged (#6508).
- **Per-model-family turn success.** `agent_sessions.deliver`
  (`projects/monolith/factory/execution/transport.py`) receives `model` but
  records no attributes. Setting `agent.model` (and `agent.model_family`:
  `codex` for sol/luna/terra/astra, `claude`, `spark`) plus the turn's
  `terminal_reason` would let `agent-turns-none-successful.yaml` be split into
  one trigger per family, which is what a Codex-only outage needs (#6509).
- **Factory tasks stuck uncertain.** No span or column exposes factory task
  state (searched for factory, uncertain, receipt, permit). A periodic span
  from the factory reconciler carrying `factory.tasks.uncertain` and
  `factory.tasks.oldest_uncertain_age_seconds` would support a trigger on
  `MAX(factory.tasks.oldest_uncertain_age_seconds) > 7200` (#6510).
