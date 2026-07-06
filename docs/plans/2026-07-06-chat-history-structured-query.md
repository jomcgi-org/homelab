# Plan: Structured, Scope-Locked Channel-History Query

**ADR:** [chat/002](../decisions/chat/002-structured-channel-history-query.md)
**Branch:** `feat/chat-history-query`
**Goal:** Give Bosun a typed, schema-constrained, scope-locked aggregation/lookup
tool over `chat.messages`, so it can answer count / first / latest and per-author
/ per-day breakdowns and exact-message lookups without ever emitting SQL.

## Design invariants (from the ADR, enforced in code)

- The tool's parameter schema has **no `channel_id` field**. Scope comes only
  from `ctx.deps.channel_id`.
- `metric` and `group_by` are **allow-listed enums** mapped to fixed SQL
  fragments/columns. An unknown value is rejected before any SQL is built.
- Every value is a **bound parameter**. No f-string interpolation of a
  model-supplied value into the statement (list members, if any, bound
  individually — mirror `search_similar`'s `exclude_ids`).
- A row `LIMIT` cap (grouped results) and a per-statement timeout bound the work.

## Task 1: `MessageStore.query_stats` in `chat/store.py`

Add one method after `search_hybrid` (raw `text()` SQL, same convention as
`search_similar` / `lexical_search`).

**Signature:**
```python
def query_stats(
    self,
    channel_id: str,
    *,
    metric: str,                 # "count" | "first" | "latest"
    group_by: str | None = None, # "author" | "day" | None
    user_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    contains: str | None = None, # FTS via websearch_to_tsquery
    message_id: str | None = None,  # exact discord_message_id lookup
    limit: int = 25,             # cap on grouped rows
) -> list[dict]:
```

**Allow-list maps (module-level constants, the security seam):**
```python
_STATS_METRICS = {"count", "first", "latest"}
_STATS_GROUP_BY = {
    "author": "user_id",                       # select username too for labels
    "day": "date_trunc('day', created_at)",
}
```
Reject `metric not in _STATS_METRICS` and `group_by not in _STATS_GROUP_BY`
(when not None) with a `ValueError` **before building SQL**. `group_by`'s value
is looked up in the dict to get a hard-coded fragment; the model's string is
never interpolated.

**Semantics:**
- `metric="count"`, `group_by="author"` → `SELECT user_id, username, count(*) n
  ... GROUP BY user_id, username ORDER BY n DESC LIMIT :limit` → who posted most.
- `metric="count"`, `group_by="day"` → `SELECT date_trunc('day', created_at) d,
  count(*) n ... GROUP BY d ORDER BY d` (cap with `:limit`) → messages per day.
- `metric="count"`, `group_by=None` → `SELECT count(*) n ...` → total matching.
- `metric="first"|"latest"`, `group_by=None` → `SELECT * ... ORDER BY created_at
  ASC|DESC LIMIT 1` → the earliest/latest matching message. (`first`/`latest`
  are only valid with `group_by=None`; reject otherwise with `ValueError`.)

**Filters** build a `WHERE` from bound params exactly like `lexical_search`:
`channel_id = :channel_id` always; append `AND user_id = :user_id`,
`AND created_at >= :since`, `AND created_at <= :until`,
`AND content_tsv @@ websearch_to_tsquery('english', :contains)`,
`AND discord_message_id = :message_id` only when the corresponding arg is set.

**Timeout:** wrap the query in `SET LOCAL statement_timeout = '3s'` within the
same transaction (Postgres only; guard so SQLite tests skip it — see Task 3).

**Return:** list of plain dicts (JSON-serializable), e.g. count-by-author →
`[{"username": ..., "user_id": ..., "count": n}, ...]`; first/latest → a single
`[{"discord_message_id":..., "username":..., "content":..., "created_at":...}]`.

## Task 2: `explore_history` tool in `chat/agent.py`

Add a new `@agent.tool` next to `search_history` (~line 448), same
`@signposted(...)` + `RunContext[ChatDeps]` shape.

**Signature (the model-facing schema — note: NO channel_id):**
```python
@agent.tool
@signposted("When someone asks for counts, rankings, or a breakdown of channel "
            "activity (who posted most, how many messages, busiest day), or to "
            "pull one specific past message.")
async def explore_history(
    ctx: RunContext[ChatDeps],
    metric: str = "count",
    group_by: Any = None,
    username: Any = None,
    since: Any = None,
    until: Any = None,
    contains: Any = None,
    message_id: Any = None,
    limit: int = 25,
) -> str:
```
- Resolve `username` → `user_id` exactly as `search_history` does (handle the
  `{'type':'user_id','id':...}` mention dict and the `_coerce_username` +
  `find_user_id_by_username` path).
- Parse `since`/`until` leniently (ISO string → datetime; ignore unparseable).
- Call `deps.store.query_stats(channel_id=deps.channel_id, ...)` — **channel_id
  from deps only**.
- Catch `ValueError` from the allow-list guard and return a short, model-readable
  message ("I can only group by author or day, and count/first/latest.") so a bad
  model call self-corrects instead of erroring the turn.
- Format the dict list into a compact human string (reuse
  `format_context_messages` for first/latest; a small table/line list for
  grouped counts). Keep it terse — this feeds a channel that hates walls of text.

## Task 3: tests

New file `chat/query_stats_test.py`, MagicMock-session pattern (mirror
`store_coverage_test.py`). Assert the **construction contract**, not live SQL:

1. `channel_id` is always in the bound params (scope lock).
2. Unknown `metric` / `group_by` raises `ValueError` and calls `session.exec`
   zero times (guard runs before SQL).
3. `first`/`latest` with a non-None `group_by` raises `ValueError`.
4. Each filter (user_id, since, until, contains, message_id) appears as a bound
   param only when supplied, and its value is passed as a param (never found in
   the SQL string) — assert the model's `group_by` string is not substring-present
   in the statement text beyond the fixed fragment.
5. A tool-level test (mirror `agent_tool_execution_test.py`) that `explore_history`
   passes `deps.channel_id` and never accepts a channel from its args.

Add the `py_test` to `projects/monolith/BUILD` (gazelle will not; repo gotcha):
```python
py_test(
    name = "chat_query_stats_test",
    srcs = ["chat/query_stats_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)
```

## Task 4: ship

- `python3 -c "import ast; ast.parse(open('projects/monolith/chat/store.py').read())"`
  and the same for `agent.py` before committing.
- `bazel/tools/format/fast-format.sh`.
- `bazel/tools/git/bump-chart.sh projects/monolith` (prompt/tool ships via the
  monolith image; bump in the SAME PR).
- Commit (Conventional Commits), push, open PR, `gh pr checks --watch`, merge
  `--rebase`, confirm ArgoCD synced.
- Notify Joe once via `monolith-monolith-agent-notify`.

## Out of scope (recorded)

- RLS channel-scope policy (`current_setting('app.current_channel')`) — phase-2
  per ADR.
- A dedicated read-only DB role — decided against for v1 per ADR.
- New `group_by` dimensions (channel-hour, weekday) — additive later, each a
  reviewed allow-list entry.
