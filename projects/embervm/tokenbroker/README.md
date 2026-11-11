# Token broker

The token broker owns mutable OAuth grants for the egress sidecar and mints
authentik service-account tokens. Credentials stay outside guests. It serves
grant login, refresh and token routes, quota observations, health and metrics,
with an optional SPIFFE mTLS listener.

## Quota observation durability

The broker persists its complete provider and grant quota snapshot in the
dedicated `<release>-embervm-tokenbroker-quota` ConfigMap. The chart creates the
object and grants the broker only `get` and `update` on that exact name. Argo CD
ignores its `/data` field and the object is not pruned or cascade-deleted, so pod
replacement and chart reconciliation do not erase runtime state. Quota data is
non-secret and is never stored beside rotating OAuth credentials.
The current production and GKE release name renders
`embervm-embervm-tokenbroker-quota`; the development release renders
`embervm-dev-embervm-tokenbroker-quota`.

Each update compares the ConfigMap resource version, reloads and merges after a
conflict, and retries at most five times. Ordering is monotonic by `observed_at`,
then broker `received_at`, then the canonical record SHA-256 for an exact time
tie. Provider and grant changes share one snapshot and one compare-and-swap.
The broker preserves both timestamps and derives age and every window's expiry
when serving a read, so startup does not make an old observation fresh.

Missing, malformed, incompatible or unavailable state restores as explicitly
unobserved. A failed write is not published to memory and invalidates cached
quota state, so it cannot fabricate healthy capacity. The POST returns 503 and
a later fresh supported report may recover once storage is available. A passed
reset only marks the relevant window expired; it never establishes healthy
capacity by itself. A newer successful report can replace the persisted
rejection without a separate scheduler or latch.

GET and metrics reads do not call the Kubernetes API. They read only the last
validated, durably committed snapshot and recompute time-derived fields. A
startup load failure or a write/reload failure marks that snapshot unreadable,
so subsequent reads stay unobserved until a newer report commits successfully.

Observed quota responses also include a read-time `pacing` object. It selects
the longest valid reported duration as the budget, so a `primary` window can
be weekly. `state` is `use_more`, `on_pace`, `conserve`, `wait_for_reset`,
`unknown`, `stale` or `awaiting_report`. Usage more than five percentage points
above elapsed budget time means `conserve`; more than five points below means
`use_more`. An exhausted short limit means `wait_for_reset` even with weekly
budget left. `blocked_until` is the latest known binding reset and is omitted
if any binding reset is unknown. A passed reset needs a fresh report.

`budget_window_minutes`, `budget_used_percent`, `budget_remaining_percent`,
`budget_resets_at`, `budget_remaining_seconds`, `target_used_percent` and
`pace_delta_percent` describe that reported budget at `evaluated_at`.
`budget_per_day_percent` divides its remaining percentage points by days to
reset. With one unambiguous shorter window, `short_window_minutes` and
`short_resets_at` carry its reported duration and actual next reset timestamp.
The broker does not infer future reset cycles or convert short-window quota
into weekly capacity.

Unknown fields are omitted. Guidance requires observations no older than
900 seconds, usage from 0 to 100, and reported durations from one minute to
one year. Missing or conflicting windows suppress spare-capacity advice;
known budget overuse can still be reported. Expired windows need another
report. These checks affect advice only: the raw observation, persistence,
exhaustion, grant ranking, admission and refresh rules are unchanged.
