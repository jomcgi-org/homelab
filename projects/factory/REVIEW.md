# Review: the factory

Rules for `projects/factory/` on top of the root `REVIEW.md`.

- **Chart before values.** Deploy values may only name fields the pinned,
  published chart renders, and the broker's config is parsed strictly. A new
  chart field or broker setting lands first; the values that use it follow
  after CI's chart write-back (#7852 → #7853, #7913 → #7915).
- **Render lock.** A change to what `deploy/values.yaml` renders regenerates
  `deploy/render.sha256` with the command `connector_test` prints.
- **Connector state.** Sends are idempotent by message ID, attempts are
  recorded before they are sent, and a fenced writer makes no external
  call. A new itsaplan or GitHub read on the dispatch path handles 401/403
  under the existing bounds.
- **What the agent sees.** A send that opens a session carries the issue
  text: agents have no itsaplan tools (#7908). Untrusted comments never
  reach a prompt.
- **Credentials.** Provider and GitHub credentials never enter a sandbox;
  they are injected at egress. A minted token names exactly the repository
  and permissions it needs.
- **Separation.** The author App never posts the review check or merges, and
  the reviewer never writes code or approves ("Merge authority").
- **Models.** A Codex model must be in the pinned Codex release's bundled
  catalogue, or the agent loses `apply_patch` (#7911).
- **GitHub reads.** Commit status creators come from
  `/commits/{sha}/statuses`; the combined `/status` endpoint has none (#7900).
