# Codex token accounting

[HOME-195](https://factory-plan.jomcgi.dev/factory/issue/HOME-195) supplies
tokens per delivered issue for HOME-130's memory comparison.

```text
verified actor JWT -> broker -> Responses API
                         |
              factory_broker_accounting
                         |
                append-only SQL functions
                         |
                  codex_call + codex_usage
                         |
             delivery_attempt -> run totals
                         |
                 itsaplan result.usage
```

The broker reads `usage.input_tokens`, `usage.input_tokens_details.cached_tokens`
and `usage.output_tokens` from JSON responses and terminal SSE events
(`response.completed`, `response.incomplete`, `response.failed`). It includes
`/responses/compact`. Input already includes cached tokens; do not add cached
input to the total again. Response IDs deduplicate replayed completions.

The observer reads each chunk before the relay writes the same bytes. It
buffers at most one 32 MiB SSE event or JSON object. It persists a completion
before forwarding its final frame or JSON closing byte, so a finished task
cannot overtake its usage write. A malformed, missing or oversized usage
record leaves the call unmeasured. Database writes have a five-second bound;
a failure logs a fixed message and increments
`factory_broker_codex_token_accounting_total` (`start_error` or `write_error`)
while the relay continues. `missing` counts successful HTTP responses without
an observed usage record. Check these counters before comparing cohorts.

The actor JWT supplies the atespace, actor name and UID. kagent v1.0.1's
[`substrate.ActorName`](https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/core/internal/substrate/client.go#L283)
is `session-` plus the lowercase session ID. Caller-provided `Session_id`
and `Conversation_id` headers do not determine attribution.

The broker records the call before the upstream request and pins it to the
latest sent delivery attempt for that namespace and session. The connector records
an attempt before sending a turn. On the initial send, the session is not yet
known; acknowledgement links its early calls. Run finalization also handles
an acknowledgement racing a call insert. Assigned calls never move to a later
run. Each run sums calls across all its delivery attempts and replacement
sessions, including failures and retries. Closure stores input, cached input
and output totals on `run`; the result outbox sends input and output to
itsaplan. Cancellation stores totals locally even when itsaplan has already
closed the run and no longer accepts a result. Connector-owned review rounds
have local totals but no itsaplan `agent_run` row.

The connector owns the migration and its dispatch writer. The broker uses
`BROKER_USAGE_DATABASE_URL` from `factory-broker-accounting-db`, with the
separate `factory_broker_accounting` login. CloudNativePG provisions this
login without database ownership, role membership or administrative powers.
The connector grants it only schema access and execution of
`record_codex_call` and `record_codex_usage`. These functions append accounting
evidence with a fixed search path and qualified table names. They cannot change
dispatch state; the login has no direct table or sequence privileges. Execution
is revoked from `PUBLIC`. Both sides of the network policy admit the broker
on the configured PostgreSQL port. No provider responses, prompts or
credentials are stored in the accounting tables. PostgreSQL and its existing
backups preserve observations across broker and connector restarts.

## Regression evidence

The following tests cover the issue's pre-rollout criteria. Dispatch tests use
real PostgreSQL, migrations run as a non-superuser database owner, and the
broker connects as `factory_broker_accounting`.

| Criterion | Evidence |
| --- | --- |
| 1: byte-preserving JSON and SSE accounting, attributed by actor | `broker/relay/tokens_test.go` tests chunk boundaries, completion-before-forwarding and unchanged response bytes. `dispatch/usage_test.go:TestRunTokenTotalsFromRelay` ignores a forged session header and excludes other sessions and namespaces. |
| 2: totals sent to itsaplan | `TestRunTokenTotalsFromRelay` drives the relay, PostgreSQL, connector outbox and production itsaplan HTTP client. The endpoint receives `usage.inputTokens=300` and `usage.outputTokens=20`, with 150 cached tokens stored locally. A second run in the same session receives only its own 400/10 tokens. `TestRunTokenResultSurvivesHTTPFailureAndRestart` checks the same payload is delivered after an HTTP 503 and writer restart. |
| 3: tokens per delivered issue | `tests/itsaplan/token-usage.test.ts` executes the operator query against the pinned upstream migrations. It uses the real result API and manual Done moves, includes failed/canceled runs, preserves unknowns, and tests reopened/archived issues and project isolation. `store/usage_test.go:TestDeliveredIssueUsage` tests the separate connector diagnostic query. |
| 4: streamed, non-streamed, missing usage and correct run | `broker/relay/tokens_test.go:TestRelayKeepsUsageWithCallingActor` checks JSON, SSE and missing usage for two signed actors, including the call IDs passed to the recorder. `dispatch/usage_test.go:TestInterleavedRunTokenAttribution` interleaves those formats across two active runs, finishes them in reverse order and checks each run's stored and HTTP totals. `TestRunWithoutResponseUsageStaysUnknown` tests both missing formats; `TestUsageBeforeSessionAcknowledgementAndAcrossRetry` tests early calls and retries. |
| Database boundary | `TestAccountingLoginCannotChangeDispatchState` rejects direct table access, state changes and role escalation, then records usage despite caller-created tables and a hostile search path. |

## Tokens per delivered issue

Run [the itsaplan query](connector/itsaplan/delivered_issue_usage.sql) in
`itsaplan`. It sums `agent_run.input_tokens` and `output_tokens` over **all
runs** of each HOME issue that reached a completed column, including failed
and canceled runs. It does not require a factory outbox receipt or a merge.
Current completed columns and the status-history `stateType` snapshots cover
manual Done moves, repeated delivery, reopening, renaming a column and
archiving. Issues with no runs remain visible. Totals stay null when no run
has measured usage.

```bash
psql "$ITSAPLAN_DATABASE_URL" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file projects/factory/connector/itsaplan/delivered_issue_usage.sql
```

The query targets the [pinned itsaplan v1.10.0 schema](https://github.com/croffasia/itsaplan/blob/6c7c2fe2cb9d810d29c68e054de25876b47434f8/packages/db/src/schema/app.ts).
`tests/itsaplan/token-usage.test.ts` runs that exact SQL after the upstream
migrations, and sends measured and unmeasured results through the real API
before checking the report. A report row is one issue, even if it reached
Done more than once. Save the report and rollout time for each comparison
cohort; historical missing usage cannot be reconstructed.

For cached-input detail and connector-owned revision rounds (which have no
itsaplan `agent_run` row), run [the connector diagnostic query](connector/store/delivered_issue_usage.sql)
in `factory_connector`. It covers the narrower set of factory-confirmed
Done moves and sums all local runs, including failed and revision runs.
The query requires a delivered `issue_done` outbox row: local `work_item.state`
becomes `done` at merge, before itsaplan accepts that move. A pending or
abandoned move does not prove delivery. Use the itsaplan query above for the
full Done cohort. Do not add the two reports together: itsaplan run IDs are
also present in the connector database.

```bash
psql "$FACTORY_CONNECTOR_DATABASE_URL" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file projects/factory/connector/store/delivered_issue_usage.sql
```

A partially observed run can have a non-null total, so also inspect the error
counters and unmeasured calls. If the call's initial database insert failed,
there is no call row to join to its run: this query cannot identify that gap.
A nonzero `start_error` counter makes the affected time window unsuitable for
a complete-token comparison; a later measured call does not repair it.

```sql
SELECT a.run_id, count(*) AS calls,
       count(*) FILTER (WHERE u.call_id IS NULL) AS calls_without_usage
FROM codex_call c
JOIN delivery_attempt a ON a.id = c.delivery_attempt_id
LEFT JOIN codex_usage u ON u.call_id = c.id
GROUP BY a.run_id
ORDER BY a.run_id;
```

An HTTP refusal, replay or interrupted stream may have no new completion usage. The
broker counts only usage the provider reports, without estimating it.

## Reproduce the accounting checks

Normal PR CI includes these targets; the upstream test creates its own
database and never connects to deployed itsaplan:

```bash
bazel test //projects/factory/broker/relay:relay_test \
  //projects/factory/connector/dispatch:dispatch_test \
  //projects/factory/connector/store:store_test \
  //projects/factory/tests/itsaplan:integration_test
```

See [the upstream test setup](tests/itsaplan/README.md) for the pinned Bun,
itsaplan and PostgreSQL fixtures and the supplemental local pytest command.
These tests establish the parser, attribution, API and query contracts.
They do not establish the live rollout result below.

## Rollout acceptance

First sync `factory-db` and `factory-db-secrets` to provision the accounting
login and its connection-string Secret. The broker's Secret reference changes,
so the chart restarts the broker. The connector applies the two accounting
migrations on startup; missing roles fail the grant migration instead of
silently disabling accounting. No database or existing login changes owner.

After the chart and both images roll out, confirm migrations
`20261117160000_codex_usage` and `20261117190000_accounting_identity`
exist in `factory_connector.schema_migrations`
and the broker's error counters stay flat. Let a newly dispatched coder run
finish. In `factory_connector`, check its run ID:

```sql
SELECT id, input_tokens, cached_input_tokens, output_tokens
FROM run
WHERE id = :run_id;
```

In the `itsaplan` database, use the same ID:

```sql
SELECT id, input_tokens, output_tokens
FROM agent_run
WHERE id = :run_id AND input_tokens > 0 AND output_tokens > 0;
```

The itsaplan query must return one row, with input and output totals equal
to the connector's. Record the run ID, totals and deployed revision on
HOME-195. A pushed branch or passing unit test does not satisfy this live
acceptance step.
