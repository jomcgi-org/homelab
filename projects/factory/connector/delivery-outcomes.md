# Delivery outcomes

Read the connector PostgreSQL database for the results of delivered and stopped PRs.
The cohort holds one row per PR. A work item that stops before it opens a PR has no row.

```sql
SELECT * FROM arm_delivery_results ORDER BY arm, task_class, guidance_version;
```

The first coder run supplies the arm, task class and guidance checkout (base SHA).
Historical runs without provenance form a NULL cohort. The underlying
`pr_delivery_results` view reads the metrics stored for each terminal PR. At closure the connector
assigns outstanding coder runs to that outcome. Replacement PRs inherit the first
coder arm and receive only the remaining runs. Late token receipts update the
outcome that owns the run. Minutes measure first send to finish per attempt.
Time held sums admission holds and observed PR hold-label intervals since the migration.
A hold that is still open when the outcome is recorded counts up to the PR's last poll,
and the outcome updates when that hold closes.
Tokens per merge divides total coder input and output across the cohort by its
merge count. Missing token receipts leave that metric NULL.
Revert detection scans base-branch commits during the first 168 hours after merge.
It accepts the forms that `factory_reverts.py` accepts: `This reverts commit <sha>`
with a full or abbreviated merge SHA, `Reverts [owner/repo]#N`, or a subject that
starts with `Revert` and cites `#N`. Recent merges
can still change the revert rate.

After rollout, check that every merged PR in the connector database has a
merged outcome. Each returned row identifies a PR to investigate. An empty
result means every recorded merge has a matching outcome.

```sql
SELECT pr.node_id, pr.number, pr.work_item_id
FROM pull_request pr
WHERE pr.merged_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pr_delivery_results outcome
      WHERE outcome.node_id = pr.node_id AND outcome.merged
  )
ORDER BY pr.node_id;
```
