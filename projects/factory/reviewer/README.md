# Factory reviewer

The service reviews open PR heads and merge groups with the reviewer GitHub
App. Postgres owns gate state; GitHub check runs are its projection. There is
no restart hold, following [Joe's decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891505565).

`DATABASE_URL` is required and comes from `factory/factory-reviewer-db`, created
by #8047. Embedded migrations create the schema. A session advisory lock and
writer epoch fence every state write. The service stops if the lock or a
database commit fails, leaving no new success check or enqueue.

The first start imports every open PR's check state and complete label
history, atomically recording completion. An unreadable check or incomplete
history prevents import and revokes existing queued merge-group passes. Those
revocation intents persist without marking import complete, including when
GitHub writes fail. Retired restart-only holds are ignored. Subsequent
starts load gate observations, approval baselines, context generations,
pending projections, merge-group conclusions and enqueue backoff from Postgres.
PRs closed during the initial import restore their recorded gate and complete
label history when first encountered later. A durable marker distinguishes
listing observations from a restored gate; failed reads leave restoration
pending across restarts. Returning to an earlier head restores that head's gate
before its cached check can authorize a pass or enqueue.

First import still has a blocking migration gap: if a legacy shared-head hold
never reached GitHub and the duplicate closes before import, the check, current
listing and label history cannot distinguish that lost hold from an unheld PR.
Recovering this case needs an additional migration witness or an explicit
one-time migration policy. Joe's accepted [issue #8048, "One-time import"](https://github.com/jomcgi-org/homelab/issues/8048)
requires clearing restart-only holds, so this implementation does not add a
blanket transition hold. Write-ahead persistence protects subsequent restarts;
it does not resolve this legacy cutover gap.

Labels, shared heads, gated paths and human-gate findings retain the existing
exact-head approval rules and authenticated `factory-review-ack:<nonce>`
acknowledgements. Every observation commits before a GitHub write. Failed
hold projections retry on the next poll; success always requires current
evidence and a fenced commit. Check publication is serialized with gate
observation within a process. Across writer takeover, each process updates
only check-run IDs it created. A new success first creates an in-progress
run, commits its ID and checks the writer fence again before completing it.
A delayed predecessor PATCH can only complete an older run; a delayed POST
can only create a nonpassing run. The successor's hold never shares an ID
with an outstanding predecessor success. Reattached tasks get a fresh check
ID before they resume, preserving their task's original idempotency key even
when its send response was lost, as well as their approval baseline.

Duplicate projections preserve a legacy head's recorded paths and human
findings until restoration completes. They cannot replace that evidence with
the provisional observations from an incomplete listing.

The deploy render lock adds the reviewer Secret's `DATABASE_URL` reference
and its TCP 5432 egress to `factory-db`. Deploy values and image pins stay as
published; the database credentials are never rendered into a ConfigMap.

`review` tests use a real Postgres instance through `factory/internal/pgtest`,
including lost GitHub writes across engine restarts, legacy import, database
failure and writer loss. `store` tests exercise lock exclusivity, epoch
fencing and migration durability. Bazel supplies Postgres to these tests in
PR CI. Local checks are `ci lint`, `ci regen`, `ci ratchet`, Helm rendering
and the chart's scoped pytest checks.

After #8047 and this chart publish, rollout acceptance verifies that restarting
`factory/factory-reviewer` leaves an unheld PR passing and enqueueable while a
PR carrying `human` remains held. Changes to the gate implementation require
Joe's merge after `factory review` shows only the human-gate finding.
