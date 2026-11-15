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

Import cannot recover observations that the old process lost from memory
without ever projecting them to GitHub. HOME-54 requires clearing restart-only
holds, so the migration does not introduce a blanket transition hold for that
unknown history. The new writer commits observations before projection, closing
that loss window for subsequent restarts.

Labels, shared heads, gated paths and human-gate findings retain the existing
exact-head approval rules and authenticated `factory-review-ack:<nonce>`
acknowledgements. Every observation commits before a GitHub write. Failed
hold projections retry on the next poll; success always requires current
evidence and a fenced commit. Check publication is serialized with gate
observation so an older success cannot overtake a newly committed hold.

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
