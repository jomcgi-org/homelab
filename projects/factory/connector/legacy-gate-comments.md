# Legacy gate comments

After verifying the updated connector rollout, the activation owner checks the connector database for rows awaiting Joe's review. Repeat the query after the connector has processed the legacy comment backlog.

```sql
SELECT id AS outbox_id, 'https://factory-plan.jomcgi.dev/issue/' || (payload->>'issueId') AS issue_link
FROM outbox
WHERE kind = 'comment' AND last_error LIKE 'assigned to Joe: verify legacy duplicate explanation%'
  AND delivered_at IS NULL AND abandoned_at IS NULL
ORDER BY id;
```

Send the returned issue links and outbox IDs directly to Joe through a channel he receives and confirm receipt. Connector notifications default to disabled. The connector checks the feed daily. The pending outbox row retains the intended body. A matching comment completes delivery. Zero tries and an empty feed can both occur while the server is processing an old POST.

When server request records establish that the original POST never ran or was definitively refused, Joe can release that specific row through the connector's at-most-once send path. Bind the verified outbox ID to `$1` in the connector database:

```sql
UPDATE outbox
SET payload = payload || '{"once":true}'::jsonb, next_attempt_at = now(), last_error = NULL
WHERE id = $1 AND kind = 'comment' AND last_error LIKE 'assigned to Joe: verify legacy duplicate explanation%'
  AND delivered_at IS NULL AND abandoned_at IS NULL AND send_started_at IS NULL;
```
