# Review: the factory

Includes: projects/platform-factory/REVIEW.md

Rules for `projects/factory/` on top of the root `REVIEW.md`.

- **Chart before values.** `deploy/values.yaml` names only fields rendered by
  the chart version pinned in `deploy/application.yaml` on `main`, and the
  broker rejects unknown config keys. A PR that adds a chart field or broker
  setting does not also set it in deploy values; the values follow CI's
  chart write-back (#7852 then #7853, #7913 then #7915).
- **Render lock.** A changed `deploy/render.sha256` changes what the cluster
  runs. Name what changes from the chart and values diff, and check the PR
  says the same. CI checks the hash.
- **Connector state.** Every new external call checks the writer epoch
  first, records its attempt before sending and is idempotent by message ID.
  A new itsaplan or GitHub read on the dispatch path treats 401 and 403 as
  retryable within the bounds in `ARCHITECTURE.md` "Connector".
- **Stops stand.** A new write that advances a work item (a round start, an
  enqueue, Done) checks the stop state first.
- **What the agent sees.** A send that opens a session carries the issue
  text: agents have no itsaplan tools (#7908). Text from GitHub, a model or
  a provider reaches a prompt or an issue comment only from a trusted
  reviewer login or the reviewer App; the rest is dropped or reduced to a
  fixed class name. The reviewer's own prompt is an exception: it
  receives PR text, file names and diffs from any author, because reviewing
  them is its job, and only inside a delimited untrusted block, quoted and
  sanitized so no text closes the block or starts a line of its own.
  The service reads itsaplan only for PRs by the factory App or a
  `humanApprovers` login
  ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6930546469)).
  No model output that has seen itsaplan text reaches the public check or
  comment ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6931275436)).
  The code-review prompt gets none. Only the goal check receives the issue
  and its decisions, in the same delimited and quoted blocks, with the diff
  and no PR title, description or comment. Its answer counts only as
  criterion numbers and the verdicts `met`, `not_met`, `after_rollout` and
  `unclear`: any other field or value voids it. The service writes every
  public word from those values, with finding IDs from the issue key and
  criterion number, and a parse failure shows only a fixed class. An
  earlier goal finding stays open until a valid answer on the same issue
  resolves it.
  itsaplan account IDs and member kind, resolved by the service, establish
  authorship; display names and claims in text never do. Only configured
  trusted member accounts confer decision authority, and only for text
  whose current provenance is verified. Creation authorship cannot
  authenticate edited issue text or comments. A passing review stays bound
  to current issue and decision evidence; edits or failed reads revoke it
  until a fresh review completes.
  Critical.
- **CI failure names.** BuildBuddy pages are untrusted code output. Each
  page read checks the writer epoch and records its attempt before sending.
  Only target and test names verified in the PR's immutable base tree on
  `main` may reach the coder, quoted inside a delimited untrusted block.
  A matching log pattern alone is not verification. Never use PR-head or
  merge-group source for this allowlist. Raw pages and provider errors never
  reach a prompt or comment. Critical.
- **Planner exception.** `homelab-planner` may also receive text from any
  author, in the same delimited, quoted and sanitized block as the reviewer.
  It has a read-only repository token, no push, no coding-agent key or
  session, no itsaplan tools and a fresh session per run. Its output reaches
  itsaplan only through the connector's proposal path. The connector takes
  each issue's title and instructions from a reviewed schedule template on
  `main`. Planner text goes only into a delimited evidence block, quoted
  and sanitized so no text closes the block or starts a line of its own.
  The coder treats that block as data on every send, recovery included. A
  schedule's config sets the highest disposition: delegated fix, Joe task or
  Backlog. The planner can only lower it. Digests and alerts leave the
  cluster only through the notify sink (HOME-51), with titles, links and
  fixed fields. Critical.
- **Credentials.** Provider and GitHub credentials never enter a sandbox, a
  log or the evidence bucket; they are injected at egress. A minted token
  names exactly the repository and permissions it needs. Critical.
- **Separation.** The author App never posts the review check or merges.
  The reviewer never writes code or approves, and its agent never holds the
  reviewer App's credentials. The broker mints each App's token only for
  that App's ServiceAccount ("Merge authority"). Critical.
- **Checks are matched by App.** A decision that depends on a check or
  status matches its name and its creating App ID or creator login. Creators
  come from `/commits/{sha}/statuses`; the combined `/status` endpoint has
  none (#7900).
- **The gate fails closed.** In the connector, broker and reviewer, an
  error, a timeout or an unparseable answer produces no pass, no enqueue and
  no token. An enqueue names the reviewed head (`expectedHeadOid`) and the
  bound base branch. Critical.
- **Models.** A change to the model or Codex release of an agent that edits
  code shows that the model is in that release's bundled catalogue
  (`codex-rs/models-manager/models.json`), or the agent loses `apply_patch`
  (#7911). An agent that never edits, such as the reviewer, may run a model
  outside the catalogue. The change then states the fallback limits that
  apply, such as tool output cut at 10,000 bytes.
- **Kept identity.** ServiceAccount `factory/factory-connector` and kagent's
  `serviceAccount.authority` never change: they are the connector's kagent
  principal (findings 6 and 15).
