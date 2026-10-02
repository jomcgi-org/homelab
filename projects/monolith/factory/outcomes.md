# Factory outcomes report

A read-only report for #6716: what each (role, model) costs, in money and wall
time, to reach a positive outcome. It is the factory half of the
jomcgi-agent-index (#6717); model-bench supplies the controlled half.

```sh
psql "$DATABASE_URL" -X \
  -v window_start='2026-09-07T00:00:00Z' -v window_end='2026-10-03T00:00:00Z' \
  -f projects/monolith/factory/outcomes_report.sql
```

Run it against a read-only connection. A CNPG hot standby works and rejects
writes. The script also sets `default_transaction_read_only`.

## Definitions

- **Role:** the `node_key` prefix (`conductor`, `implement`, `review`,
  `refine`, `correct`, `investigate`). `conductor_funding_*` is reported as
  `funding`.
- **Attempt ok:** `swarm_node_run.status = 'succeeded'`. This is the node's own
  verdict, not the task's outcome.
- **Positive outcome:** the task merged (a `merged` audit) and was not
  reverted (no `reverted` audit). CI conclusion comes from the `merged`
  audit's `ci.conclusion` once landing records it. Until then `ci_green` is
  blank, which means unknown, not red.
- **Cost:** a bracket.
  - `list_usd` sums the attempt's own priced turns
    (`agent_turns.list_cost_usd`). This is a lower bound: turns that ended in
    `error` record no usage, so they carry no price.
  - `reserved_usd` is the `factory_start` ledger, where an unsettled start
    counts its `max_cost_usd` cap. This is an upper bound.
  - `unpriced_turns` shows how wide the gap is.
- **Size band:** additions plus deletions of the merged PR, from
  `observability.merged_prs`. S is under 50, M is under 300, L is everything
  else. Unmerged tasks have no PR, so they land in `unknown`.

## Reading it

The report shows when routing is too weak: a (role, model) pair with a low ok
rate, many unpriced error turns, or a high cost or time per success.

It cannot show when routing is too strong, because the factory only records
the model that was assigned. Whether a cheaper model would also have succeeded
is a counterfactual, and model-bench answers it by running every model on the
same tasks.

Compare models only within one size band.

Policy generations change which models each role can use (for example, the
Claude 5.5 models joined on 2026-09-30). Pick windows that match the
generations you are comparing.
