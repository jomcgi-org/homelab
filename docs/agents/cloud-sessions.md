# PRs from a cloud session (no GraphQL)

In a Claude cloud session GraphQL is blocked and `gh` may be absent, so
`gh pr create`, `gh pr view`, `gh pr merge --auto` and review-thread commands
fail. Use GitHub REST with `curl`; the session proxy supplies auth, and every
write needs `-H "Content-Type: application/json"`.

- **Open a draft PR:** `POST /repos/{owner}/{repo}/pulls` with
  `{"title","body","head","base":"main","draft":true}`.
- **State, mergeability, head:** `GET /repos/{owner}/{repo}/pulls/{n}`, fields
  `state`, `mergeable_state` and `head.sha`. Compare `head.sha` with
  `git rev-parse HEAD` after a push.
- **CI:** `GET /repos/{owner}/{repo}/commits/{sha}/status`, context
  `pr-checks`. Its `target_url` is the BuildBuddy invocation;
  `docs/agents/ci-triage.md` says how to read its log without the MCP.

The proxy also exposes `ccr` routes for what REST lacks, all under
`/repos/{owner}/{repo}/pulls/{n}/ccr/`:

| Route | Does |
| ----- | ---- |
| `PUT auto_merge` / `DELETE auto_merge` | enqueue in the merge queue / dequeue |
| `POST ready_for_review` | draft to ready |
| `POST convert_to_draft` | ready to draft |
| `GET review_threads` | list review threads |
| `POST comments/{id}/resolve` | resolve the thread holding comment `{id}` |
