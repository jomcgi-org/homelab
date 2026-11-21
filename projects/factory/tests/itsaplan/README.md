# Factory itsaplan integration tests

`//projects/factory/tests/itsaplan:integration_test` runs in normal PR CI.
It exercises the chart adapter against itsaplan v1.10.0's real route guard,
authorization, comment, issue-update and run-enqueue services. PostgreSQL
triggers fail or suppress the enqueue after the delegate write to verify
transaction rollback and recovery. Other cases check concurrent retries,
agent-to-agent delegation with `runnerScope: team`, eligibility and recovery
after a full Todo column has capacity again.

`token-usage.test.ts` sends results through the real agent-run result route
and checks the persisted `input_tokens` and `output_tokens`. It executes
[`report-delivered-usage.sh`](../../connector/itsaplan/report-delivered-usage.sh)
against that database and a second database with all connector migrations.
The real `psql` export, temporary import and consolidated query count a shared
300-token run and connector-only 200-token revision as 500, preserve unknown
revision runs, recover local cancellation totals, and stop on database errors.
The other cases cover successful, failed, canceled
and unmeasured runs; manual Done moves; repeated delivery; reopened and
archived issues; column changes; issues without runs; and project isolation.
No connector outbox or mock itsaplan schema decides the report's cohort.

```sh
bazel test //projects/factory/tests/itsaplan:integration_test
```

`MODULE.bazel` pins the upstream commit. `fixture.bzl` fetches Bun 1.4.4 by
SHA-256 and installs the API's dependencies from upstream's frozen lockfile
with lifecycle scripts disabled. This happens during repository fetching,
before the test runs. The fixture archive preserves Bun's package links.
The test itself needs no downloads, Docker daemon or external service.

The launcher extracts a writable upstream copy, mounts the same four adapter
files as the chart and creates a private PostgreSQL 16 from `@postgres_test`.
It chooses a local port, creates and migrates `factory_test` and `connector_test`, runs the suite
and stops PostgreSQL in cleanup. Credentials are synthetic. Caller-supplied
database URLs are ignored. Missing fixtures or any suite failure fail the
target; the test has no skip or manual tag.

The decision probe runs the connector handler against the pinned API and
checks the saved comment author, text and delegate in PostgreSQL. For local
pytest, build the probe and export its absolute path in `DECISION_PROBE`
(`go build -o /tmp/decisionprobe ./projects/factory/tests/itsaplan/decisionprobe`). Supply `BUN_BIN`, `ITSAPLAN_ARCHIVE` (an archive of the
pinned checkout with its locked API dependencies installed), `PG_BIN` and
`PG_SHARE`. The same launcher still creates and deletes its own database.

```sh
python3 -m pytest -q -s projects/factory/tests/itsaplan/integration_test.py
```

The chart's `deferred_test` target also retains the portable in-memory
fault-injection suite. Revalidate both targets before changing the pinned
itsaplan release; the adapter refuses other versions at startup.
