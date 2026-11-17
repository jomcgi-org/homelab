# Agent reference

Task-specific details for `AGENTS.md`. Read the section needed for the work.
Root merge authority applies throughout these procedures.

## Commands

```bash
ci              # lint changed files + selective regen + remote Linux test
ci lint         # format only files changed vs origin/main
ci regen        # generators only, and only when inputs changed
ci test         # affected tests on one hosted Linux runner
ci test -- //...  # explicit full-suite escape hatch

helm template <rel> projects/<svc>/chart/ -f projects/<svc>/deploy/values.yaml
```

`./bootstrap.sh` then `direnv allow` puts `ci`, `helm`, `crane`, `go`,
`python`, `pnpm`, `node` and the formatters on PATH.

- Use `ci`, not bare `bazel` or `bazelisk`: a Mac has no matching remote
  executors. `bb remote` is allowed. Targeted `pytest` on pure-Python files
  you edited is fine as an advisory check.
- **Never pipe `ci` or `bb remote` output into a filter or discard it.** Run
  unpiped or `| tee` to a file. `ci test` has exited 0 without running
  anything (#4118): judge a run by its `Executed N out of M tests` line.
- **BUILD generation uses the repository's pinned Gazelle.** Factory source
  guests use the packaged Linux `//:gazelle_binary` during complete pre-push
  validation. Ordinary local `ci regen` still leaves BUILD generation to CI.
  Never substitute PATH Gazelle. Fetch and rebase after any CI writeback.
- **Generated sources needed by ordinary tooling belong in a clean checkout.**
  Keep pinned Bazel generation authoritative, seed only the required outputs
  as tracked files, and verify their bytes against regenerated outputs in
  Linux CI. Reuse the trusted branch autoformat writeback with `git add -u`,
  after parallel formatters finish; never nest Bazel in `run-generators.sh`.
  Keep main and merge-queue no-write guards. Do not check in other build
  artifacts. The EmberVM Go protobuf targets are the pattern (#7066).
- Image push happens only in CI on merge to main.

## Delivery mechanics

These commands are for the role that owns implementation or landing.
Factory workers use the assigned branch and return their role's artifact;
they do not launch their own planner, review or landing sequence.

- For standalone edits, use a dedicated branch and worktree, or a new branch
  through connected GitHub tools. Keep the assigned branch for factory work.
- Run `ci` before pushing when available, otherwise use PR CI. Read failed
  logs before retrying. Never push to a merged branch.
- The landing owner checks required CI and independent approval against the
  current PR head. On main, that is `pr-checks` and the reviewer App's
  `factory review` check. A failing `factory review` lists its findings, so
  fix and push. On a PR authored by `jomcgi`, a human-gate finding clears
  through the verified authorship waiver in `REVIEW.md` "Joe decides"
  ([decision](https://github.com/jomcgi-org/homelab/issues/8049)). Review guidance:
  `docs/agents/review.md`.
- `homelab pr land <n>` follows merge, chart writeback and rollout. Otherwise,
  `gh pr merge <n> --auto` enters the merge queue; pass no strategy flag.
  Rebase only for conflicts or CI's branch writeback, not because main moved.
- Queue entries can have `autoMergeRequest: null`; inspect
  `mergeQueueEntry{state}`. Read an ejection's failure before re-enqueueing.
- The #7450 rollback implementation is staged default-off. Durable rollout
  failure creates a recovery receipt only when enabled. Its worker reverses
  the full landed PR range and creates or adopts `revert(<scope>): ...` with
  `Reverts #N`. Ordinary admission, required Linux checks, independent exact-head
  review and queue ordering still apply. Landing reopens the original issue
  with evidence and records the reverted outcome. Source holds and human
  ownership stay binding. The revision gate stays off; operational acceptance
  remains on #7450 with the authorized landing and validation owner.
- For deployable changes, wait for chart writeback and call
  `verify_deployment` with the expected revision until terminal. Without it,
  verify ArgoCD Synced/Healthy, the new pod image and a responding service.
  Docs need no rollout.

## Gotchas

- **For `monolith`, `monolith-agents`, `monolith-public`, `embervm` and
  `scotland-connector`, `targetRevision` in git does not say what is deployed.** Kargo owns it on the
  hub. The `projects/gke-apps/` pins are hand-edited floors kept as the revert lever, so
  `git != live` is correct there. Read the live value:
  `kubectl get application monolith -n argocd -o jsonpath='{.spec.sources[0].targetRevision}'`.
  Every other chart deploys off the git value.
- **Never hand-pin `@sha256:` digests for this repo's images in values
  files.** Build-time pinning replaces tags; hand pins go stale into
  `ImagePullBackOff`. Gated. Third-party digest pins are fine.
- **Never hardcode a `.svc.cluster.local` URL.** Helm prepends the release
  name, so a rename silently breaks it. Read it from an env var set in
  `values.yaml` (`envOr("URL", "")`, no default). Gated.
- **Monolith endpoints that read cluster resources need matching `ClusterRole`
  verbs.** A missing verb fails in prod as `Forbidden`, which dashboards show
  as a generic 5xx.
- **Keep bulk data out of `chart/migrations/*.sql`.** The migrations ConfigMap
  has a 256 KiB client-side annotation cap; load seeds out of band
  (`projects/monolith/hikes/seed/`).
- **Grep the tests before changing a number.** TTLs, timeouts, `max_tokens`
  and retry counts are asserted; update the assertions in the same change.
- **Images are apko plus `rules_apko`, never Dockerfiles**, amd64 only (all
  callers pass `arm64 = False`; the macro default is `True`),
  non-root uid 65532 with `runAsNonRoot: true`. Re-adding arm64 needs
  `arm64 = True` and per-arch `tars`: `arm64 = False` with `multiarch_tars`
  fails only at push.
- **Python deps are `@pip//package` via `aspect_rules_py`.** `requirement()`
  does not exist here. JS is pnpm plus `rules_js`. The
  `projects/monolith/frontend/` app is Svelte 5 runes only, with CSS imported
  from JavaScript, never bare `@import` package specifiers inside CSS.
- **Cluster reads** use `kubectl` against the hub context, or the monolith
  MCP's `k8s-*` tools and `verify_deployment`. The ArgoCD UI has no route;
  Kargo's is `private.jomcgi.dev/app/kargo`. MCP topology:
  `projects/mcp/ARCHITECTURE.md`.
- **New service:** a chart and `deploy/` under `projects/<svc>/` (copy
  `projects/monolith/deploy/` for the multi-source pattern), plus a hub
  Application in `projects/gke-apps/<svc>/` listed in its `kustomization.yaml`.
  `ci regen` does not create the hub Application.

## Tracker

itsaplan project HOME, at `https://factory-plan.jomcgi.dev/factory/HOME`
behind Authentik, holds outstanding work. An issue's link is
`https://factory-plan.jomcgi.dev/factory/issue/HOME-N`, and a comment's adds
`#comment-<id>`. Columns are 1 Backlog, 2 Todo, 3 In Progress, 4 Done and
5 Canceled; priorities are `urgent`, `high`, `medium` and `low`.

A session on Joe's account uses the API as `joe-sessions` ("Joe (sessions)"),
signing in with `joe-sessions@jomcgi.dev`. itsaplan 1.10.0 allows no hyphen in
a human username, so its mention handle is `@joesessions`. It is a regular
member of HOME; it has no admin role in the factory workspace and is not an
agent. Record Joe's decisions as `joe` or this sessions account and link the
record from the HOME issue. `factoryadmin` is reserved for bootstrap and
break-glass recovery; its comments are never evidence of Joe's decisions.
Dispatched agents have no itsaplan access; their issues come from the connector.

### Ticket shape

Follow [docs/writing.md](../writing.md) and
[Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-126#comment-841).

- **Title:** state the outcome as an instruction in 70 characters or fewer.
- **Description:** use 200 words or fewer, in this order:
  1. The `Refs` line: "Put `Refs HOME-N` in the commit message."
  2. **Problem:** three sentences or fewer.
  3. **Goal:** one sentence.
  4. **Decisions:** state each decision the coder needs and link its record.
  5. **Out of scope:** name the excluded work.
- **Flows and state machines:** use a small diagram in a code block.
- **Done when:** create one checklist with this exact title. Each item states one check a reviewer can verify.
- **Work larger than one PR:** create sub-tasks, one PR each.
- **Order between tickets:** use `blocked_by` links. Do not write instructions such as "delegate after HOME-X" in prose.
- **Long design or reference text:** put the full record in an itsaplan document. Summarize required design details in the description and link the document.
- **Logs and screenshots:** add attachments. State the relevant errors, reproduction steps and visible results in **Problem**.
- **Joe's decisions:** record each in a comment by `joe` or `joesessions`. State the decision under **Decisions** and link the comment.

Dispatched agents cannot read itsaplan documents, comments or attachment contents.
The connector sends attachment names only. Before delegation, put all required
context in the description and use **Done when** for the checks. Links preserve
the source records for human readers. If the required context exceeds the ticket
limits, split the work into smaller sub-tasks before delegation.

### API access

- Port-forward the API: `KUBECONFIG=~/.kube/factory.yaml kubectl -n factory
  port-forward svc/factory-itsaplan-api 13000:3000`.
- Sign in with `POST /api/auth/sign-in/email`, using fields `email` and
  `password` from Secret `factory/factory-itsaplan-joe-sessions`, read with
  `KUBECONFIG=~/.kube/factory.yaml`. Keep the values and session cookie out of
  command output and logs. Send header
  `Origin: https://factory-plan.jomcgi.dev`, keep the session cookie, and
  call `POST /api/auth/sign-out` when done. Sign-in is rate-limited (429), so
  use one sign-in per batch of calls.
- Check `GET /me` returns a `user` with `name: "Joe (sessions)"`, `username: "joesessions"`,
  `role: "user"` and `isAgent: false` before writing. After the first rollout,
  post a comment on HOME-59 mentioning `@joe` and confirm its author is
  "Joe (sessions)". Do not fall back to `factoryadmin` if this sign-in fails.
- Read the board with `GET /projects/HOME/issues/board`; user IDs are under
  `assignees` in `GET /projects/HOME`.
- Write with `POST /projects/HOME/issues` (`title`, `description`,
  `columnId`, `priority`, `assigneeUserId`, `delegateUserId`),
  `PATCH /issues/<id>`, `POST /issues/<id>/comments` (`body`) and
  `POST /issues/<id>/links` (`targetIssueId`, `kind`: `blocks`,
  `blocked_by`, `relates`, `duplicates`).

Setting `delegateUserId` to the coding agent (`homelab-codex`) starts a run
with the ticket's current content. Complete the ticket before delegation. The connector holds
the run, without using an agent slot, until each `blocked_by` issue is in a
completed column (HOME-55). A canceled blocker or a cycle keeps the hold and
gets a comment; complete the blocker or change the link. Never predict a new issue's
key: other sessions create issues concurrently, so read it from the response.

## Human handoff

A HOME issue in Todo or In Progress that is assigned to Joe with no delegate
is Joe's: no run starts on it, and the "Needs Joe" view
(`/factory/HOME/view/1`) lists it.

To take delegated work from the factory, stop it with
`connector cancel <ISSUE> --by joe`, or move the issue to Canceled and leave
it there, and close its PR: a closed PR cannot merge, and reopening it is the
new owner's choice. Changing the assignee or removing the delegate stops nothing. Wait
for the stop to settle before anyone else touches the work. The connector's
database covers itsaplan runs and its own review rounds, which itsaplan has no
record of:

```sh
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory-db exec factory-db-1 \
  -c postgres -- psql -At -d factory_connector -c "
  SELECT w.state,
    (SELECT count(*) FROM run r WHERE r.work_item_id = w.id
       AND r.state <> 'closed') AS open_runs,
    (SELECT count(*) FROM cancellation c JOIN run r ON r.id = c.run_id
       WHERE r.work_item_id = w.id AND c.state = 'pending') AS pending,
    (SELECT p.number FROM pull_request p WHERE p.work_item_id = w.id) AS pr
  FROM work_item w WHERE w.issue_key = 'HOME-N'"
```

The stop has settled when the state is `stopped`, `open_runs` and `pending`
are 0 (a cancellation is confirmed only after kagent confirms the task
stopped), and GitHub shows the PR closed or merged
(`gh pr view <pr> --json state`). The connector's own PR fields are its last
poll and can be stale. When the query shows no PR, look on GitHub for one
from the issue's branch, which the connector may have opened after its last
write: `gh pr list --head factory/HOME-N --state all`. Close any open one.
A PR sync that began before the stop can still create a PR, even after the
connector timed out. The sync checks the stop only at its start, then makes
up to three GitHub calls, each with a 30-second client timeout
(`projects/factory/connector/github/client.go`). GitHub ends any request it
has not finished within 10 seconds
([Timeouts](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#timeouts)).
So wait five minutes after the stop settles, run the search again and close
any open PR it finds. Only then is the handoff settled. If
`factory-db-1` is not the primary, take the name from
`KUBECONFIG=~/.kube/factory.yaml kubectl -n factory-db get cluster
factory-db`.

Then file a new HOME issue, assigned to Joe with no delegate, that links the
canceled one and names what remains. To hand the work back, delegate that
new issue.

On a PR, `human` means a person owns it and `needs-human` asks for a decision.
Both hold the PR at the connector and the reviewer, and agents must not add or
remove them without an explicit handoff instruction.

A PR that hit its round bound (`maxReviewRounds` or `maxRebaseRounds`, 6
each) waits for a person. The connector records the refused event as
`round_limit` and never reads it again, so raising the bound restarts nothing
by itself until HOME-109 lands.

A `jomcgi` changes-requested review restarts a PR only when two things hold:
its work item has review rounds left (`review_rounds < maxReviewRounds`, for
example after the bound was raised), and no earlier `jomcgi` changes-requested
review acted on the same head. A second one on that head counts as a
duplicate. In that case:

1. Check that its head is unchanged and no run is open (the query above).
2. Post the review on that exact head. Give it the reviewer's blocking
   findings and the failing CI targets, because the coding agent can't read
   BuildBuddy. The round prompt keeps 4000 characters. On a conflicting PR,
   the round also tells the agent to rebase.
3. Once `review_event` shows the review as `acted`, dismiss it, because
   a trusted changes-requested decision keeps the PR out of the queue. If it
   shows `round_limit` or `duplicate`, nothing started: use the paths below.

On a PR whose only blocker is the reviewer's hold
(`factory-review:human-gate-only`), a changes-requested review reinstates the
hold, and the connector ignores every event while it stands. There, approve
after the changes-requested review instead of dismissing it, and only when
Joe has cleared that hold.

In any other state, a review can't restart the PR:
- **Review rounds are spent:** raise the bound through a reviewed values
  change first.
- **The head has already been used:** a new head gives the connector new
  events to act on.

Otherwise, stop the work as above and file what remains as a new HOME issue.

The retiring hub factory reads the same labels on GitHub issues:

Label an issue `human` when Joe or another person takes ownership, including
implementation, coordination or observation acceptance. This label excludes
both delivery and advisory refinement even when `agent-ready` is present.
Add it before removing `agent-ready`; leave a comment naming the owner, why
work is handed off and what remains. Assignment is useful but not required.
`needs-human` means the factory needs a decision; `human` means a person owns
the work. Agents must not add or remove `human` without an explicit handoff
instruction, relabel owned work `agent-ready`, or start competing work.

The factory fences new starts after observing `human`, reconciles existing
attempts and their costs, then records cancellation with `human_handoff`
evidence. This is neither a delivery success nor an escalation. Unknown
invocations and reservations remain fenced until normal reconciliation proves
their outcome. A handoff is sticky for an admitted receipt: removing the label
does not resume it. Return work through explicit factory re-admission after
the old task settles; removing `human` only restores intake eligibility.
Issues and existing PRs stay open for the human owner.

## Knowledge and production evidence

Use `search_knowledge` for recurring failures; confirm results against current
code and live state. Report durable findings with evidence and dispute facts
shown wrong. Unavailable MCP tools are not a blocker; briefs for other agents
must include the evidence they need.

For production behaviour, use Polylane when available and read its review
comment before landing production-path changes. Follow
`docs/runbooks/argocd-outofsync.md` for rollout failures.

## Where to look next

Files under `docs/agents/` are procedures for specific jobs. Open one only
when its row applies.

| When | Read |
|------|------|
| Working under `projects/monolith/`, `projects/monolith/web/grimoire/` or `projects/embervm/` | that directory's `AGENTS.md` |
| Factory orchestration, execution or controls | [FACTORY.md](../../projects/monolith/factory/orchestration/FACTORY.md) first; [monolith section 4](../../projects/monolith/ARCHITECTURE.md#4-agents) for ownership, trust, rationale and acceptance |
| A red CI run you need to diagnose | `docs/agents/ci-triage.md` |
| Opening, readying or enqueuing a PR without GraphQL (cloud session) | `docs/agents/cloud-sessions.md` |
| Running Go or pytest checks in a cloud session without `ci` | `docs/agents/local-tests.md` |
| Proposing or judging a factory improvement | `docs/agents/evolution.md` |
| Reviewing a finished PR diff | `docs/agents/review.md` |
| Shipping a feature | `docs/agents/ship.md` |
| Writing a PR title and description | `docs/agents/pull-requests.md` |
| Refreshing a system's STPA model | `docs/agents/stpa.md` |
| Queueing or triaging drain-lane (`qwen-drain`) jobs | `docs/agents/drain-queue.md` |
| Cutting BuildBuddy cache traffic | `docs/agents/buildbuddy-usage.md` |
| Security-sensitive change | `docs/security.md`; `docs/THREAT-MODEL.md` for open findings |
| Public tier: jomcgi.dev, monolith-public, `public_reader` data | `docs/runbooks/public-tier-checklist.md` |
| ArgoCD OutOfSync, stuck rollout, "is my change live?" | `docs/runbooks/argocd-outofsync.md` |
| Adding a service | "New service" in Gotchas above, `projects/platform/ARCHITECTURE.md` section 4, `projects/gke-apps/kustomization.yaml` (the live workload list) |
| Observability or alerting | `docs/observability.md` |
| Frontend or design: tokens, palette, motion, a11y | `.impeccable.md` |
| Any writing: docs, PRs, commits, comments, tickets, chat, site copy | `docs/writing.md` |
| Operator changes | `projects/operators/best-practices.md` |
| How a domain works today | `projects/<domain>/ARCHITECTURE.md`; each `**Why.**` paragraph carries the rationale |
| Build, CI, tooling, hooks, Semgrep | `bazel/ARCHITECTURE.md` |
| Recording a decision | Joe's decision from an account that is his alone ("Tracker" above), linked from its HOME issue; then a `**Why.**` paragraph in the domain's ARCHITECTURE.md linking it and a Direction row with its HOME issue; no ADRs (#4667) |
| Filing, delegating or updating work | "Tracker" above |

**Runbooks** (`docs/runbooks/`) are explicit-only: open one when Joe names it,
a row above points at it, or a task asks for it. Index:
`docs/runbooks/README.md`.

**Claude Code hooks** (`.claude/settings.json`) duplicate CI and git gates to
fail fast; a rule every author must follow lives in CI or a git hook.
