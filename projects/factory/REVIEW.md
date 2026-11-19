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
  retryable within the bounds in the [credential refusal retries](connector/ARCHITECTURE.md#credential-refusal-retries).
- **Stops stand.** A new write that advances a work item (a round start, an
  enqueue, Done) checks the stop state first.
- **What the agent sees.** A send that opens a session carries the issue
  text: agents have no itsaplan tools (#7908). Text from GitHub, a model or
  a provider reaches a prompt or an issue comment only from a trusted
  reviewer login or the reviewer App; the rest is dropped or reduced to a
  fixed class name. The reviewer's own prompt is an exception: it
  receives PR text, file names and diffs from any author, plus comments
  and check output from the Semgrep App. These reach it only inside
  delimited untrusted blocks, quoted and sanitized so no text closes a
  block or starts a line of its own.
  The service reads itsaplan only for PRs by the factory App or a
  `humanApprovers` login
  ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6930546469)).
  No model output that has seen itsaplan text reaches the public check or
  comment ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6931275436)).
  The code-review prompt gets none. Only the goal check receives the issue
  and its decisions, in the same delimited and quoted blocks, with the diff
  and no PR title, description or comment. Before reporting a missing fact,
  it searches the reviewed commit, including definitions outside the diff.
  Coders can add public repository references by criterion number in
  `.factory/goal-evidence/<issue-key>.md`. The reviewer checks each claim
  against that commit without running PR commands. Only verified facts
  affect the verdict. On a later head it also receives
  the previous head's goal verdicts and private reply on the same issue
  evidence, quoted, and the names of the files changed since. Its answer counts only as
  criterion numbers and the verdicts `met`, `not_met`, `after_rollout` and
  `unclear`: any other field or value voids it. The service writes every
  public word from those values, with finding IDs from the issue key and
  criterion number, and a parse failure shows only a fixed class. An
  earlier goal finding stays open until a valid answer on the same issue
  resolves it, or until Joe's rule withdraws it: goal blockers may clear
  ([HOME-157](https://factory-plan.jomcgi.dev/factory/issue/HOME-157#comment-3904),
  [HOME-318](https://factory-plan.jomcgi.dev/factory/issue/HOME-318#comment-3905),
  [HOME-327](https://factory-plan.jomcgi.dev/factory/issue/HOME-327#comment-3906)).
  When the PR names a different, readable issue whose trusted text has a
  countable criteria list, and the goal check's valid answer numbers the
  same count, the old issue's goal findings are withdrawn, and the note
  names both issues. When the trusted issue text
  drops a criterion, its finding is withdrawn: the service counts the items
  of the issue's one "Done when" or "Acceptance criteria" list, only when
  the issue text has a trusted author and editor, and the valid answer
  must number the same count. A numbering gap or any other ambiguity
  counts nothing, as does an issue with an HTML tag start, triple backtick or
  triple tilde: comments, code fences and HTML can hide or fake list items. An issue with a diagram keeps
  main's rules. So does a thematic break, a setext underline, or an item
  with no words on its marker line. An indented heading is code, not a
  heading. History replay
  withdraws nothing. A restated criterion gets a fresh verdict under the same
  ID. A missing issue reference, an uncounted list or a count the answer
  disagrees with withdraws nothing; a finding then clears only when a
  valid answer marks it met. The
  goal answer has no follow-up field, so no follow-up issue key reaches the
  public check. The goal check's reasons stay private: only the connector
  reads them from the reviewer's internal listener, posts them on the issue
  and quotes them in the coder's round as untrusted data, with no `@`
  mention and never on GitHub
  ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-251#comment-2324)).
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
  itsaplan only through the connector's proposal path, or through the
  [triage actions](connector/triage.md) for a
  schedule whose reviewed values list them. The connector runs only the
  listed ops, checks each precondition against its own state first and holds
  every write credential. Model text sets no title, description or
  instruction (HOME-386,
  [decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-386#comment-3915)).
  The connector takes
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
- **Complete review state.** A saved review keeps every blocking and model
  gate finding's full ID and severity. Missing history blocks across later
  heads and restarts; a new report or approval alone cannot clear it.
  Recovery replays original reports from a complete checkpoint or a complete
  history, preserving the identity scheme of each original report. If a
  legacy task expired, a record below both storage caps with distinct,
  unshortened IDs can recover findings. Such records cannot prove resolutions:
  later reports must explicitly account for every recovered finding. A
  failed GitHub or kagent read keeps the check blocked and retries on the
  next poll. History that is lost or cannot be read in full (an expired
  task with a lossy record, a truncated timeline, too many heads, ambiguous,
  foreign or undecodable records) keeps the check blocked and carries every
  known finding. No review or approval clears it; open a new PR from commits no
  review has seen (rebased or squashed onto main). A review
  whose state overflowed stays blocked until a replay of its task recovers
  its findings. A later run on a head that keeps no state
  does not hide an earlier run's state. Failed replacement checks retry on
  the next poll. A GitHub check-write outage can leave old PR-head and
  merge-group passes usable; rollout must drain the queue before replacing
  legacy checks (#8009).
  Critical.
- **Models.** A change to the model or Codex release of an agent that edits
  code shows that the model is in that release's bundled catalogue
  (`codex-rs/models-manager/models.json`), or the agent loses `apply_patch`
  (#7911). An agent that never edits, such as the reviewer, may run a model
  outside the catalogue. The change then states the fallback limits that
  apply, such as tool output cut at 10,000 bytes.
- **Kept identity.** ServiceAccount `factory/factory-connector` and kagent's
  `serviceAccount.authority` never change: they are the connector's kagent
  principal (findings 6 and 15).
