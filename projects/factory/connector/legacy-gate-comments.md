# Legacy gate comments

Joe owns unresolved legacy comment sends. The connector sends him the issue link and checks the feed daily. The pending outbox row retains the intended body. A matching comment completes delivery. Zero tries and an empty feed can both occur while the server is processing an old POST.

When server request records establish that the original POST never ran or was definitively refused, Joe can release that specific row through the connector's at-most-once send path. Bind the verified outbox ID to `$1` in the connector database:

```sql
UPDATE outbox
SET payload = payload || '{"once":true}'::jsonb, next_attempt_at = now(), last_error = NULL
WHERE id = $1 AND kind = 'comment' AND last_error LIKE 'assigned to Joe: verify legacy duplicate explanation%'
  AND delivered_at IS NULL AND abandoned_at IS NULL AND send_started_at IS NULL;
```
