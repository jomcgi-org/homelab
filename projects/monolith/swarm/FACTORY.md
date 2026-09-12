# Bounded issue delivery lane

The first autonomous lane reads explicitly selected GitHub issues, records one
durable receipt per repository/issue/generation, and admits up to the policy's
per-lane `max_tasks` at a time, with the chart's
`swarm.factoryMaxConcurrentTasks` (4 today) bounding the sum of the two lanes.
`max_tasks` is a concurrency, not a lifetime count: as tasks settle the lane
keeps admitting until its issue list is exhausted, so it runs without an
operator re-arming it.
A planning session in Ember builds the whole graph at plan time and is called
back only when the plan deviates. The server reconciles the mutable graph and
dispatches each admitted node as an independent DBOS workflow with immutable
inputs. Planning does not run in the monolith process.

This slice prepares a reviewable PR. The broader factory conductor in #5784,
shared admission across all execution surfaces, other incident feeds and
autonomous landing retain their own acceptance gates. A successful node is not
evidence that its task delivered the requested outcome.

## Availability and operator policy

The chart's `swarm.factoryEnabled` defaults to false. It makes the reconciler
available, while a separate durable operator transition enables admission.
The factory control migration starts disabled. The HTTP control surface under
`/api/swarm/factory` requires a verified standing human principal in the
`operators` group. Forwarded email headers and agent credentials do not grant
operator authority.

Configure an explicit policy, then enable it through `/control`. All policy
fields are required. An example for one approved issue is:

```json
{
  "repo": "jomcgi-org/homelab",
  "issue_numbers": [1234],
  "generation": 0,
  "max_tasks": {"delivery": 1, "advisory": 1},
  "max_task_turns_hard": 40,
  "max_parallel_nodes": 1,
  "task_budget_usd": 60,
  "turn_budget_usd": 5,
  "allowed_models": ["opus", "luna"],
  "conductor_model": "opus",
  "worker_model": "luna",
  "base_branch": "main",
  "turn_timeout_seconds": 900,
  "task_timeout_seconds": 14400,
  "max_attempts": 2,
  "max_review_rounds": 2
}
```

The plan sizes the task and policy keeps the envelope. `max_task_turns_hard`
and `task_budget_usd` are what an accepted plan has to fit inside; the number of
delivery starts a task actually gets is derived from the plan the conductor
accepted. `max_turns_per_task` was the old fixed cap and is still accepted: a
policy that carries it and no `max_task_turns_hard` reads it as the envelope, so
a live policy needs no re-post. A policy must carry one of the two.

`max_parallel_nodes` is how many nodes one task may hold in flight at once and
defaults to 1, so a policy written before fan-out existed stays serial until an
operator raises it. Conductor planning rounds are capped separately by the
optional `max_planner_turns`, which inherits the envelope when it is omitted.
Planning rounds still draw on `task_budget_usd`. The optional
`max_review_rounds` bounds the review correction rounds the engine runs on its
own and defaults to 2. Those, `reviewer_model`, `model_pools`, `quota_guard`,
and `intake` are the only optional fields; every other field is required.

### Two lanes

A task runs in one of two lanes and its lane follows its class, which is not
configurable. Work that ends in a pull request is delivery: `bug-fix`,
`mechanical-refactor`, `docs`, `judgment-analysis`. Work that ends in a comment
is advisory: `refine`, `advisory-diagnosis`, `advisory-triage`.

`max_tasks` bounds each lane on its own:

```json
{"max_tasks": {"delivery": 1, "advisory": 2}}
```

A bare integer is still accepted and reads as the delivery lane, which is the
capacity it always asked for, so a live policy needs no re-post. In that shape
the advisory lane is 0 and no advisory task is admitted at all: **an operator
running `refine` has to post a lane map to keep it running.** The advisory lane
is opt-in on purpose, because the number that used to bound it was bounding
delivery too.

Tasks in flight also draw on the shared background session pool, which the
drainers and the synthetic probes draw on too, so
`swarm.factoryBackgroundReserve` (default 2) is the number of slots the factory
will not take. The factory is the only member of that pool that can wait for
nothing: a node the pool declines stays ready and starts on a later tick, so the
factory is the one that yields. Every node is gated on it, the first node of a
settled graph included.

`swarm.factoryMaxConcurrentTasks` bounds the sum, and **it must be set at least
as high as delivery plus advisory.** When it covers their sum, each lane simply
has its own maximum. Below their sum the lanes contend for it, and the free part
of the ceiling is dealt one slot at a time to whichever lane is furthest from
its own maximum, with delivery taking the first slot so it is never left unable
to start anything. The sweep audits `lane_ceiling_below_lanes` with the ceiling,
both maxima and their sum, at most once an hour, so an operator sees which of
the two numbers is actually binding.

Reserving delivery's whole maximum first, as the limits used to, is what starved
advisory work: at a ceiling of 4 with lanes 4 and 8 on 2026-09-11 the advisory
lane had zero room and one sweep excluded 272 candidates as `lane_full`. The
chart ceiling is now 12, which covers the 4 and 8 the lane runs today, and the
split is the safety net for a ceiling somebody sets low. At a ceiling of 1 the
advisory lane still cannot open at all. Note that `admit_next` takes the oldest
queued receipt across the lanes that have room, so under a contended ceiling a
backlog of older delivery receipts can still take most of it; raising the
ceiling, not the split, is the fix for that.

Admission and intake both work per lane. A full delivery lane no longer refuses
an advisory admission, and intake ranks candidates exactly as it did, delivery
before refine, but fills at most one candidate per lane per tick rather than
one candidate in total. A candidate whose lane is full is counted as
`lane_full` in the idle audit. The board shows each lane as used against limit.

### Reviewer fallback while the Claude window is spent

Every delivery task ends in an independent Opus review on the shared Claude
subscription, and that window is the one input the factory can exhaust:
implementation autoscales and the cheap implementers bill elsewhere. The answer
is to review on a cheaper model, not to stop delivering. The optional
`quota_guard` block sets the thresholds:

```json
{"quota_guard": {"claude_7d_pause_percent": 85, "claude_7d_resume_percent": 75}}
```

Both default to those numbers, so a policy that predates the block still routes.
Both are whole percentages between 1 and 100, and the resume threshold must be
strictly below the pause threshold: equal thresholds are a flap, not a guard.
Naming only one is allowed as long as the pair stays ordered.

At or above the pause percent, review nodes run on the next member of
`model_pools.reviewer` that has provider quota, default `["opus", "astra"]`.
Opus comes back on its own once the window falls below the resume percent.
**Nothing else changes.** Delivery admission is never held, implement nodes on
Sol or Muse run exactly as they did, and the advisory lane is untouched.

Reviewer independence is a property of the session a review runs in, never of
the model it runs on. A fallback reviewer still runs in its own session and is
still refused if that session is the implementer's, which is the check
`verify_delivery` has always made. Delivery evidence records the model that
approved, so an accepted PR says who reviewed it rather than leaving it to be
inferred from the date. That model is read from the immutable dispatch pin, not
from the review artifact, because the artifact is written by the agent and
cannot be authority on its own identity.

The model is chosen when the review is dispatched, not when it was planned. A
plan written while the window was quiet can reach its review hours later, so
the node keeps the model the planner asked for and the pin records what really
ran. A retry after the window moved is a new attempt and takes a new pin.

Two things never fall back:

- **Judgment work waits.** `judgment-analysis` has a capability floor rather
  than a price, so its review runs on Opus or waits for it.
- **An empty pool waits.** When no member has quota, review waits rather than
  falling further. Review is the gate, and a gate that lets itself be skipped
  is not one. Astra and Sol share one Codex grant, which is why the default
  pool stops at Astra: a Sol rung could never be reachable when Astra is
  walled, and offering it would make the fallback look deeper than it is.

A waiting review is not a paused task. The work waits where it stands and
starts on the tick a reviewer has quota again, audited `review_waiting` once
per node per window transition with the models it skipped and why.

The routing reads the 7-day window the token broker already observes for the
`claude` provider, once per tick behind a short cache. Transitions are audited
`reviewer_fallback` and `reviewer_restored`, one row each, and the board renders
the state from that ledger without a broker call. An unknown reading, or one
older than an hour, never starts a fallback: it audits `quota_guard_unknown` at
most hourly and review stays where it is, because downgrading every review
whenever a broker read fails would turn one outage into two. It does not end a
fallback either, so a fallback entered at 95 percent does not snap back to Opus
the moment the broker goes down.

### Model pools

`model_pools` carries up to four ordered preference lists, and `select_model`
takes the first member whose provider still has quota.

| Pool | Routes | Default |
|---|---|---|
| `conductor` | planner rounds | the `conductor_model` alone |
| `worker` | every other planner-added role | the `worker_model` alone |
| `implement` | implement nodes, engine corrections, engine fan-in | the `worker` pool |
| `refine` | the advisory briefing node | `["spark", "sol"]`, narrowed to allowed models |
| `reviewer` | every review node, planner-added and engine-inserted | `["opus", "astra"]`, narrowed to allowed models, led by `reviewer_model` |

A role pool must start with that role's own configured model, so the policy's
stated preference is what the pool is ranked from. A class pool, `implement`,
`refine` and `reviewer`, names no policy field and so has no head to anchor to; its order
is the preference. Every member of every pool must appear in `allowed_models`.

The `refine` default only applies to what a policy allows: a policy that allows
neither `spark` nor `sol` falls back to the conductor pool, which is where
refine ran before class pools existed. `spark` needs no separate guest profile,
because the Muse family lands on the same `claude-runtime` session workload as
Claude and Codex; its plaintext egress lane and endpoint transport pin are
guest-side and already shipped (#5978).

The judgment floor searches the `implement` pool first, then the conductor
pool, then the conductor model, so judgment work keeps its Opus-class floor
whichever pool routes the rest. The floor ignores quota on purpose: a
quota-walled Opus holds judgment work rather than demoting it.

The `reviewer` pool leads with the policy's own `reviewer_model` when that is
not already in it, so a policy naming one reviewer and no pool keeps exactly
the reviewer it asked for and gains a fallback behind it. A planner may name a
review model only from this pool; anything else is refused as
`reviewer_model_mismatch`.

### Shared session admission

Factory nodes draw execution permits from the same pool as the work-queue
drainer and the synthetic probes. The bounds are chart values under
`agentSessions.admission`: `total` over every tier, `background` over the
non-interactive tiers under it, and `kg` under that. The code defaults are
4/3/2 and the chart sets 16/12/2. A narrower `total` clamps the two inner
numbers.

`total` must stay at or below what EmberVM will actually create for the session
workload: `claudeRuntimeWorkload` concurrency `cap` in
`projects/embervm/deploy/values.yaml`, 16, with `session.maxSessions` at 24. It
is `total` that has to fit rather than `background`, because every model family
except pi lands on `claude-runtime`, so an interactive permit reaches the same
workload. Above the cap a granted permit meets a `workload_cap` denial at guest
create, which fails the turn rather than making it wait.

### Autonomous intake and refine

The optional `intake` block is fully defaulted when an older policy does not
carry it:

```json
{
  "enabled": false,
  "labels": ["agent-ready"],
  "exclude_labels": ["needs-human", "wontfix", "security-finding"],
  "max_per_day": 5,
  "cooldown_hours": 24,
  "refine_enabled": false
}
```

`auto_merge` is a separate top-level policy field rather than part of this
block, because it governs what the lane does after a delivery is approved
rather than what it takes in. It is optional and defaults to `false`.

`enabled` controls autonomous issue discovery. `labels` names delivery-ready
labels and may be empty, in which case no issue is a delivery candidate.
`exclude_labels` rejects an issue before selection. `max_per_day` counts
delivery admissions only and is between 1 and 10000, and `cooldown_hours` is
between 1 and 168. `refine_enabled` allows an
otherwise eligible issue with none of the delivery labels to enter the refine
path. `close_enabled` allows a refine verdict to close an issue and
`max_closes_per_day` bounds how many it may close in a rolling 24 hours,
default 3. All three feature flags default off or, for the cap, bound a
capability that is itself off.

Intake sweeps GitHub at most once an hour while it is finding nothing, again
immediately after any receipt settles, and again on the tick after it admits
anything. That last clause is what lets a lane fill: a sweep takes at most one
candidate per lane, so on the hourly clock alone four delivery and eight
advisory slots filled at one slot an hour. It costs at most one extra sweep per
admission, because the sweep stamps its own clock before reading GitHub, so a
sweep that admits nothing leaves the newest admission behind the newest sweep
and the hourly clock governs again. A tick with no room in any lane returns
before it reaches the clock at all. A tick that cannot read GitHub audits
`intake_error` rather than failing silently. The sweep reads open issues
and open pull requests oldest first, up to five pages of one hundred each; a
read that hits that cap records `truncated` on the audit, so a partial sweep
reads as partial.

Intake admits at most one issue per lane per tick, and never opens more than
`max_per_day` deliveries over a rolling 24 hours. The cap bounds delivery
churn, which is pull requests and the Opus reviews they cost. Advisory
admissions are uncounted: a refine writes a comment for cents and buys no
review, so letting one consume the cap would throttle a burn-down for nothing.
A capped tick refuses the delivery candidate and audits `daily_cap` once, and
still admits an advisory candidate on the same tick. The board reads
`admitted_today` the same way, as deliveries. A failed or cancelled receipt
cannot be selected again until
its cooldown expires, whatever class it settled under. Assigned issues,
excluded labels, issues linked from an open pull request, issues already
delivered, and issues already received in the current generation under the same
task class are not candidates.

An issue with a `succeeded` receipt in any generation and any class is excluded
as `delivered` and is never admitted again. The lane's own labels are not
evidence that work is outstanding: #3877 shipped as PR #6007, the pull request
body carried no closing keyword, the issue stayed open with `agent-ready`
intact, and the next generation's sweep read it as fresh work. The exclusion is
unconditional, including for an issue a human reopened. GitHub's issue listing
carries no reopen timestamp and `updated_at` moves on every comment, label and
edit, so it cannot tell a reopen from a comment, and establishing the difference
would cost one events read per candidate on a request budget the whole lane
shares. An operator who wants a delivered issue worked again names it in the
policy `issue_numbers` allowlist under a new generation, which writes the
receipt directly and never consults this sweep. Issues closed on GitHub are
excluded as `not_open`, which the open-issues listing already implies and
selection asserts independently. Scoping that last one to the class is what lets an issue a refine
pass moved to `agent-ready` be delivered in the same generation, with no
operator bumping `generation` to release work the lane itself made ready. The
receipt's identity carries the class for the same reason. Intake-created receipts become admissible only while
`intake.enabled` is true. Turning intake off therefore leaves the operator's
`issue_numbers` allowlist exactly as it was.

Delivery candidates rank before every refine candidate. Within either group,
`critical` ranks before `bug`, then the oldest issue ranks first. A refine
candidate carrying `critical` never outranks a delivery candidate without a
rank label.

Each intake receipt carries the task class that sets its verification mode,
implementer floor, and gate (ADR agents/038 decision 5):

| Class | Verification | Floor | Gate |
|---|---|---|---|
| `bug-fix`, `mechanical-refactor`, `docs` | machine-verified | the worker pool | independent Opus review plus required CI |
| `advisory-diagnosis`, `advisory-triage`, `refine` | advisory | the worker pool | none, because nothing merges |
| `judgment-analysis` | judgment | Opus or better | independent Opus review plus a human spot check |

Both intake paths classify. An issue an operator names in `issue_numbers` is
read for its labels exactly as a discovered one is, so the judgment floor
applies whether the lane found the work or was told to do it. Classes come from
the issue's labels in this order:
`security-finding` and `needs-thought` select `judgment-analysis`, `bug` selects
`bug-fix`, `documentation` selects `docs`, and `todo` selects
`mechanical-refactor`. `security-finding` remains excluded by default. An
unclassified `agent-ready` issue defaults to `bug-fix`. A receipt written
before classes existed also reads as `bug-fix`, so operator-posted receipts are
unaffected.

The judgment floor is a capability constraint. A quota-walled Opus holds
judgment work instead of demoting it. The floor searches the implement pool
first, then the conductor pool, and falls back to the conductor model when
neither names an Opus-class member, so a policy whose pools carry no such model routes
judgment work to its strongest configured model and says so in the node's
stated reason. Configure an Opus-class worker pool member before enabling
intake on a repository whose issues carry `needs-thought`. Advisory classes other than `refine` have
no producer yet and park the task paused; phase 3 fills that hook.
Reviewer-driven class escalation and the verdict ledger are deliberately not
built in these phases (#3843).

A refine task runs one planner-class node with at most two attempts and has no
DAG. The node posts exactly one `## Agent brief` comment with `### Outcome`,
`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that order,
then reaches exactly one of four verdicts and acts on it:

| Verdict | What the node does | What settlement demands back from GitHub |
|---|---|---|
| `agent-ready` | applies `agent-ready` | the label, the brief, and the issue still open |
| `needs-human` | applies `needs-human`, ends the brief with `### Decision needed` carrying `recommend: deliver \| close \| split \| defer` and the one question a person must answer | the label, the brief, the issue still open, and one warn notification naming the issue, the recommendation and the question |
| `reject` | adds `### Why not` citing a file, pull request or recorded decision, applies `wontfix`, closes with reason `not_planned` | the label, the brief, and the issue closed |
| `stale` | adds `### Why stale`, applies `stale`, creating the label if the repository has none, closes with reason `not_planned` | the label, the brief, and the issue closed |

`reject` and `stale` are for a premise a reader can check: a decision recorded
in an ARCHITECTURE.md **Why.** paragraph, work already merged, a file or flag
that is gone. The prompt says that doubt resolves to `needs-human` with a
recommendation rather than to a close, because a wrong escalation costs a
minute and a wrong close costs the issue.

Closing is gated twice. It needs `close_enabled`, and it needs room under
`max_closes_per_day`, counted from the `intake_closed` audits of the last 24
hours. Both are read before the node runs, so the prompt offers three verdicts
rather than four when closing is unavailable, and again at settlement, so a cap
spent while the node was running still holds. A close verdict the lane may not
act on is downgraded to `needs-human`: the server then demands the
`needs-human` label and an open issue, exactly as it would for an escalation,
and records `refine_close_downgraded`. An issue carrying `critical` or
`security-finding`, or assigned to any milestone, is never closed and takes the
same downgrade. Every close that settles audits `intake_closed` with the
evidence cited.

The server settles from a re-read of the issue, never from the node artifact
alone. A verified `needs-human` result succeeds because the briefing and the
escalation both completed, and a verified close succeeds because the issue is
demonstrably closed with its reason on record. A mismatch fails the task and
the issue takes the cooldown. Two failed attempts settle the task failed and
apply nothing.

### Escalation decisions

A `needs-human` verdict is a decision waiting on a person, so it arrives as
options rather than as a question. The artifact carries two to four, ordered
with the recommendation first, and the server refuses the settlement when they
do not hold up:

```json
{
  "key": "split",
  "label": "Split the operator console out of the API",
  "effect": "split",
  "detail": {"children": [{"title": "...", "body": "..."}]}
}
```

`effect` is one of five. `agent-ready` applies the delivery label and takes
`needs-human` off, with an optional scope note posted as a comment. `close`
comments the reason and closes with `not_planned` or `completed`. `split`
opens one to five child issues, then closes the parent against them. `defer`
applies `needs-thought`, takes `needs-human` off, and comments the condition
that would make the work worth doing. `hold` writes nothing and records that
someone looked and chose to leave it.

The first option is the recommendation, and its effect has to be the one the
`recommend:` line names: deliver is `agent-ready`, close is `close`, split is
`split`, defer is `defer`. The prompt asks for labels that name the concrete
act ("Close as superseded by #5656") rather than the verb the effect already
carries, because the label is the whole of what a person reads before
deciding. The same options are the numbered list in the brief's
`### Decision needed` section, so a reader on GitHub and an operator on the
console are choosing from one list.

A close verdict the server downgrades never wrote options: the node reached a
verdict it was allowed to act on and the server is what turned it into an
escalation. Settlement synthesises two for it, the close the node wanted and
`hold`, so every escalation is decidable rather than leaving the downgraded
ones as a question with no buttons under it.

The escalation is stored on the receipt as `escalation_json`, which is also
where the resolution lands, and the options are repeated on the
`refine_settled` audit as what was offered at the time.

**The escape group.** Every unresolved escalation also carries three fixed
options the board view appends rather than a brief writing them, so an
operator who agrees with none of the offered options can leave the card
without a chat round trip and the wait for another brief. `escape:close`
closes the issue as `not_planned` and takes `needs-human` off. `escape:defer`
swaps `needs-human` for `needs-thought`. `escape:dismiss` resolves the
escalation on the factory side with no GitHub write at all, so the card leaves
the list while the issue keeps `needs-human` and intake goes on skipping it.
All three record the operator's note on the resolution, and the first two post
it as the comment. The keys carry a colon, which the option key pattern
forbids, so a brief can never author one that collides; the brief's own
options keep the numbers 1 to 4 and the escapes answer to `x`, `d` and `Esc`.

`escape:close` is never weighed against the protected-label rule that
downgrades a node's own close on a `critical` or `security-finding` issue.
That rule exists so a node does not close one of those unwatched, and the
operator clicking here is the authority it was deferring to. It is the one
escape the page confirms before sending, because it is the one no other button
on the card undoes.

The close comments, closes, then drops the label, in that order. A failure
between the close and the label leaves a closed issue still carrying
`needs-human`, which nothing acts on. The other order leaves an open issue
with the label gone, which is exactly the state that puts it back in front of
intake.

**Deciding.** `POST /api/swarm/factory/decisions/{receipt_id}` behind the same
operator gate as `/control`, with `{"option_key": "...", "note": "..."}` or
`{"action": "chat", "note": "..."}`. The private agents page at
`/agents/escalations` reaches the same code through
`POST /api/agents/factory/decisions/{receipt_id}`, which the browser can use
because it is gated on `X-Auth-Email`, the address Envoy projected from the
verified Access JWT and the gateway strips on ingress so it cannot be
smuggled. Not `Cf-Access-Authenticated-User-Email`: nothing in the cluster
validates or strips that one, so a caller reaching the backend can set it to
any address. A request carrying no projected address, two of them, or only the
Cloudflare header is refused.

Cloudflare Access is the gate. `private.jomcgi.dev` is zero trust locked to one
identity, so an address arriving on the projected header was already authorised
to be there and a second list would only restate that.
`FACTORY_OPERATOR_EMAILS` is empty by default and any single verified identity
decides. Set it only to narrow that: non-empty it is an allowlist, so someone
Access admits who is not on it reads the board and the escalations and gets a
403 on the click.

That list, once it is set, is a secret and not a values entry. Provision it as a
key on the `monolith-chat-secrets` 1Password item and wire it with
`valueFrom.secretKeyRef` the way `GITHUB_API_TOKEN` is wired, rather than naming
operator addresses in a public `deploy/values.yaml`.

A decision is refused while the receipt is `admitted` or `uncertain`, because a
brief running on the issue would keep writing to something the decision has
just closed or relabelled. The page shows that card as `briefing` with its
buttons disabled rather than offering a click the server would refuse. The
escape options are refused on the same terms: the reason is the live node, and
it does not care which option the operator picked.

Every write is idempotent on the pair of receipt and option. A comment carries
a hidden marker naming that pair and is skipped when the marker is already on
the issue, a label add is idempotent already, and each child a split opens is
fenced by its own `decision_child_created` audit row, so a retry after a
network failure finishes the decision rather than doubling it. A decision is
claimed before it is applied, which is what refuses a second, different option
against one escalation; repeating the same option returns the first result.
A claim is superseded by a later `decision_failed` naming the same option, so
one failed GitHub call does not lock the escalation to the option that failed.
Every failure is audited, including the ones an effect raises from inside
itself. Claims and child records are matched on the receipt id carried in the
audit detail rather than on `task_id`, which is null for a receipt waiting on
a re-brief and would otherwise match every other receipt in that state.
Applying audits `decision_applied` with the actor, the option and what it did.

**Chat.** `action: chat` posts the note on the issue prefixed
`Operator asks:`, records it on the escalation, audits
`decision_chat_requested`, and returns the refine receipt to `queued` so the
lane briefs the issue again in the same generation with the note in its
prompt. The receipt rather than a new one, because admission selects on the
policy's generation and a receipt at any other generation would never be
admitted at all. The issue text is never rewritten. This is the one path that
returns a settled receipt to the queue, so total spend per generation is
bounded by the receipts a generation can hold plus the re-briefs an operator
asks for by hand.

The re-brief replaces the escalation document rather than adding to it. The
second brief settles onto the same receipt, so keeping the first one would
leave the page showing options written before the question was answered, and
pressing 1 would apply a stale first option. The chat history carries forward,
because it records what was asked rather than any one brief's answer.

The question is posted whether or not the lane can take the re-brief, so the
response says which happened. `requeued: false` carries `blocked_by` naming
the reason: not a refine receipt, a brief already queued or running, a
generation the policy has moved past, or an issue that is neither in the
policy allowlist nor discoverable with intake on. The card renders it, because
a question on an issue with nothing scheduled to answer it looks exactly like
one that was taken.

Deciding after asking for chat is allowed and cancels the re-brief: the
resolution returns a `queued` receipt to `succeeded`, so the lane never spends
an advisory slot briefing an issue that is already closed, split or labelled
for delivery.

The `needs-human` warning on Discord carries the escalations link, so the
notification is the way in rather than a thing to read and then go looking.

Before enabling `refine_enabled`, confirm the guest GitHub token has
`issues: write` on a fine-grained token, or `repo` on a classic one. Opening a
pull request needs `pull_requests: write` or `repo`. A fine-grained token
granted only `pull_requests: write` and `contents: write` can push and open pull
requests but receives 403 on refine comments and labels. That 403 becomes a
failed refine attempt rather than silent success because settlement re-reads
the issue. Set `FACTORY_EXECUTOR_LOGIN` to a non-empty GitHub login to require
the verified brief comment to come from that author. The chart does not set it
today, so that check is inert until an operator does. Settlement still requires
a `## Agent brief` comment posted at or after admission, and reads the comments
written since then rather than only the first page, but without the login it
accepts such a comment from any author.

The derived allowance is `max_attempts` summed over the live nodes that have
not succeeded, plus the work turns already spent, plus what the engine may still
insert on its own. Review rounds are reserved lazily, one round at a time: the
next round only, at the two turns it really costs, because the engine inserts
its correction and its re-review at one attempt each. A correction that fails
costs the round rather than the turn: the engine opens the next round against
the same reviewed head and the same findings, and the failed round still counts
against `max_review_rounds`. One attempt is a deliberate trade: a correction or
re-review that fails or stalls spends a round rather than retrying silently
inside a round nobody sized for it, and the planner is asked once the rounds
are spent.
Reserving every remaining round up front instead priced a loop the task would
probably never open, and it left a nine-turn envelope unable to hold a
three-node plan at all.
Each round the engine inserts is then counted like any other live node while the
round behind it is reserved, so the allowance grows by one round at a time. A
fan-out wave still reserves its one fan-in node at `max_attempts`, exactly as
the inserted node will carry, so a fan-in is turn-neutral. Review rounds are
reserved only when the plan holds a review node. Its dollar figure is the same
sum over node `max_cost_usd` ceilings plus charged history. It is stored on the receipt as
`allowance_json` with the graph revision it came from, re-derived and audited
whenever an accepted edit changes the graph, and it is the bound work starts
meet.

The reserve is headroom for sizing the planner's next edit, not a charge against
the engine's own. Every insertion is checked against the envelope as it happens,
and an engine insertion, a review round or a fan-in, is checked on the nodes it
really adds with no forward reserve counted on top: otherwise a round the
envelope can afford is refused because of a round that may never open. The
forward reserve is recomputed after the insertion, for the planner's view. An
edit whose derived allowance would exceed the envelope is refused whole with
`envelope_exceeded` and a detail naming needed against allowed for both turns
and dollars, beside `spare_turns` and `spare_usd`, what the envelope would still
fund once the reserve that edit brings with it is counted. The planner reads
that in `decision_feedback` and answers by shrinking the edit to fit whichever
is binding, splitting the work, or pausing; it must not re-propose a refused
edit unchanged. Discarding a node drops its unspent slots and never refunds a
consumed turn.

The example is documentation, not live authorization. Use the selected issue,
current capacity and an explicitly accepted policy for an operating trial.
Admitted tasks retain their policy even if later configuration changes.
The initial worker model uses the proven Luna route. Muse can be selected after
its actual dispatcher and guest route are verified on the deployed stack.

## Execution and evidence

Each graph admission and factory turn reservation commit atomically. The
node's deterministic session identity is reused after an interrupted submit.
A workflow replay cannot read a different graph or silently replenish its
attempt, task-turn, deadline or budget bounds. Confirmed failed artifacts feed
bounded retry context back to the next attempt. Unknown execution retains its
reservation and requires reconciliation before another attempt starts.

The DBOS application version is pinned to the node workflow's own source:
`execute_node`, the helpers that decide which steps run, and every step they
call. DBOS otherwise derives it from every registered workflow in the process
and neither recovers nor dequeues anything an older version started, so any
unrelated workflow edit stranded in-flight nodes. Pinned, a deploy that leaves
those functions alone recovers its in-flight nodes natively. The cost is that
another workflow changed in a deploy now keeps its version and is recovered
against its recorded steps, which DBOS refuses loudly with
`DBOSUnexpectedStepError`, and a loud refusal beats the silent PENDING strand
this replaces.

Editing the node workflow or one of its steps is therefore the one deploy that
strands in-flight nodes, and nothing can recover them. The reconciler cancels
such a workflow, audits `workflow_stranded` with both versions, and settles the
attempt as uncertain, after which the node's real session outcome is reconciled
and the node retries within `max_attempts`.

A node whose workflow is PENDING on the current version but whose newest
`dbos.operation_outputs` checkpoint is older than its `turn_timeout_seconds` is
stalled rather than stranded. A healthy node checkpoints every poll and every
sleep, so that gap means the workflow stopped progressing. It is settled exactly
like a stranded one: `node_stalled` audited once per workflow, one Discord
warning, the workflow cancelled, and the attempt settled uncertain. Cancelling
is what makes the workflow terminal so stop supervision can confirm the guest
ceased, and a node that then fails with no retry left reaches the planner
through the ordinary deviation path rather than a planner node queued behind
the stalled run.

Settling either one as uncertain does not start stop supervision on its own.
Supervision runs only once the attempt's stop is due: an operator stop, a
cancellation request, the turn timeout elapsed measured from dispatch, or two
minutes after the session's own turn was recorded as failed. That last term is
what makes the deadline track the guest instead of the policy. A turn timeout
bounds how long a turn may run, so it was the right deadline only for a turn
that could still be running, and supervision never sees one:
`read_uncertain_factory_attempt` refuses the attempt unless its turn is already
terminal with `terminal_reason` `error`, so a live turn is out of scope upstream
and the failure stamp is always there. Waiting out the turn timeout was
therefore waiting for a turn that had already ended. The two-minute grace is
there so the conductor's own native completion check settles the attempt first
where it can. Every term is a minimum, so the grace only ever brings a stop
forward. A strand or
a stall caught early still holds its reservation until one of those deadlines
passes rather than self-healing at the moment it is observed.

A replica lost mid-invoke used to cost the attempt outright. The rollout
cancelled the executor watching the turn, the guest carried on working, and the
executor recorded an unknown invocation that failed the session and left stop
supervision to destroy a guest that was in the middle of the work. The guest
publishes its complete native record to a result receipt before it writes the
synchronous response, so that record outlives the observer. An invoke whose
response is lost while the control plane still shows the guest running, with an
invoke started and no invoke completion, is now held rather than settled: the
turn is marked interrupted with stop reason `response_lost`, its pending row
keeps its claim so nothing re-dispatches the prompt, and its permit keeps its
state so nothing releases capacity the guest is still using. DBOS workflow
recovery already brings the node back on the new replica, so the recovered
node's next dispatch poll finishes the attempt from that receipt through the
ordinary turn writer, and the conductor's late-completion reconciliation does
the same before deciding the attempt has no result. The model runs once, and
the node owner supplies the declared artifact path a hold reconstructed from
durable rows cannot know, so a recovered attempt keeps its artifact.

Neither owner ends a hold on its own judgement. A guest that has ceased, that
completed its invoke without ever publishing, or that has moved on to another
invoke or another generation, can no longer produce the evidence, so the hold
becomes the ordinary unknown outcome the existing reconciliation settles. A
guest still invoking keeps waiting. Whether a guest is still invoking is the
order of two control-plane stamps rather than the absence of one:
`invoke_started_at` is stamped per invoke and strictly increases, while
`last_invoke_at` is stamped on completion and is never cleared, so an invoke is
in progress when its start is later than the last completion. Reading a missing
completion as "in progress" would have held a guest's first turn and no turn
after it.

Every hold is bounded by the invoke budget clamped to the twelve-hour workload
backstop, after which the claim lease settles it unknown exactly as it did
before. There is one hold per dispatch, ever: an attempt whose marker is
already a hold is refused a second one, or an unrecoverable result would be
re-held at every lease pass until the receipt's own seven-day retention and
pin the admission slot for a week rather than twelve hours. A committed body
that can never be adopted, one that is not a native completion, ends its hold
at once rather than waiting out a bound that cannot change the answer.
Recovery is behind `agents.sessions.responseLostRecoveryEnabled`, off by
default and dependent on `resultReceiptsEnabled`: with no receipt to adopt, a
hold would only delay the same unknown outcome. Every owner that writes,
finishes or ends a hold reads that one flag, so off is the behaviour this lane
had before any of this existed (#5938, #4322).

An attempt whose workflow died mid-way has no session recorded on its run,
because `record_dispatch` binds one only at completion. The reconciler resolves
it by the deterministic `local_session_id`, `factory:<task>:<node>:<attempt>`,
under the same ownership checks `reconcile_completed_node` applies, and binds
it, so supervision can start. A repeated observation of unknown execution
records nothing: the first uncertain outcome stands until reconciliation makes
it terminal.

Missing provider usage consumes the entire reserved ceiling. This is
conservative admission accounting, not an interruptible dollar cap on a running
provider turn. Observed overruns prevent further admission.

A planner decision is one graph edit or one `plan` whose edits apply together
under a single expected revision, so a rejected edit rejects the whole plan and
the graph never holds half of one. Each edit becomes its own plan version under
the shared cause, applied in dependency order rather than in the order written,
and an edit may name a dependency by the key its author wrote where the server
can resolve that to a role-prefixed key without guessing. Review correction is the server's, not the
planner's: when a review returns `changes_requested` the reconciler appends
`correct_<n>` on the model that produced the reviewed head and `review_<n>` on
the configured independent reviewer, up to `max_review_rounds`. Those keys are
refused to a planner, and the rounds are counted from the version ledger, so
discarding or renaming a correction node cannot buy another one. A round that
fails is reopened by the engine for the same reason it was opened by it: the
planner is refused those keys and `swarm/graph.py` refuses discarding a node
that has run, so a failed `correct_<n>` would otherwise leave the task with no
task-local recovery path at all. The replacement round carries the same reviewed
head and findings, and the failed round still counts, so a round that keeps
failing spends the bound instead of looping inside it. A node that escalated is
deliberately not reopened, because escalation asked for the planner.

Reopening cleans up after the round it replaces. The failed round's re-review
never ran and never can, so it is discarded in the same atomic edit, which
returns its attempt and its ceiling to the allowance; its correction stays,
because it has runs and the graph refuses discarding those. A correction can
also push and then die, so the replacement is briefed at the task branch head
read live at insertion, with the findings still attributed to the earlier head
the review inspected, and it falls back to that head when the branch cannot be
read. Once a later round supersedes it, a `correct_<n>` or `review_<n>` stops
being scanned as an open failure, or a task would be handed `node_failed` on a
node the planner may neither add nor discard at the moment the round that
replaced it delivered. When the bound is spent the deviation still names the
review, and it now also names the last correction and why it delivered nothing.

Each round inherits its timeouts from the nodes it repeats: the correction takes
the reviewed implementation node's `turn_timeout_seconds` and the re-review
takes the original review's, each clamped to the policy ceiling. The planner is
asked to size every node it adds to the work that node really does, roughly 900
to 1800 seconds for investigation, 3600 to 7200 for implementation and 3600 for
review, rather than leaving the policy maximum in place.

The correction brief states its completion contract: commit on the task branch,
push, confirm the pull request head moved, and write the declared JSON artifact.
A guest with no local test tooling records that in the artifact and pushes
anyway, because the required Linux CI that gates the work runs on the pull
request and not in the guest. A turn that ends with neither a push nor an
artifact fails the round.

The planner is
called back only for a named deviation: no plan applied yet, a node that failed
or escalated with no runnable retry, exhausted review rounds, or a settled
graph with no verified delivery.

Nodes that can start together are dispatched together, up to
`max_parallel_nodes`. That set is a wave: source-writing nodes that have never
run, whose dependencies are all already on the task branch, and that no
dependency path connects to each other. Each member works on
`factory/<task-id>-<node key>`. That is a sibling of the task branch, not a path
under it, because git cannot hold `refs/heads/factory/<task-id>` and a ref below
it at once. An `integrate` node merges those branches into the task branch,
resolves conflicts, runs the targeted checks and reports the integrated head;
review then examines that head and the correction rounds work on the task
branch. The planner may add the integrate node itself; otherwise the engine
inserts `integrate_<n>` over the wave and repoints its dependents at it, so every
branch the engine handed out is one the fan-in merges. A node takes a branch of
its own only once that fan-in exists, so a refused insertion asks the planner
rather than stranding work, and the branch a node first ran on is pinned for
every later attempt of it. `integrate_<n>` is reserved to the engine exactly as
`correct_<n>` and `review_<n>` are. While a wave is open it is the only source
of source-writing work that may start, so a node outside it waits its turn
rather than writing the task branch beside the wave. Extra concurrent nodes are
admitted only when the shared session pool has room, and a node the pool cannot
hold stays ready for the next tick rather than failing. At a limit of one none
of this applies and the lane is serial, a planner-authored integrate node
included.

The guest hydrates the existing task branch, or the base branch before the
task branch exists. Source changes belong in a dedicated linked worktree on the
branch its brief names. Transient typed artifacts must be written under
`/workspace/src/.factory/`, where the shim captures them. Current dispatch uses
validated complete added-file diffs; the existing whole-file channel is accepted
only when its stored path and validation metadata match the declared artifact.

Completion requires a non-draft PR on the exact task branch, a successful
implementation node, the latest independent review approving the current head
on a model the reviewer pool allows, and passing repository PR status checks. GitHub is read again to verify
the head and checks. Delivery evidence remains in the task audit.

The pull request body must close the task's issue. Every delivery node's
boundary states the required `Closes #<issue>` line and says to keep it on
every update to the pull request, engine correction rounds included, and the
completion gate reads the body back from GitHub and refuses a delivery that
does not carry a closing keyword. The refusal is named
`pr_missing_close_keyword`, so the planner reads what is wrong instead of a
generic validation failure and can ask for the body to be fixed. The gate
accepts every keyword GitHub acts on, `closes`, `fixes` and `resolves` in all
their forms, because refusing a body that says `Fixes #123` would fail a
delivery that does close its issue, and all three reference forms GitHub
honours: `#123`, `owner/repo#123` and the full issue URL. Only this task's own
issue in this task's own repository counts.

### Landing

Landing is off unless the policy sets `auto_merge` to `true`, and only the
value `true` counts: a policy written before the flag existed, or one carrying
anything else, lands nothing. It is the first factory step that writes to the
repository rather than reading it, so the process needs a GitHub token with
write access to pull requests and issues before the flag is worth turning on.

With the flag on, a task that settled `succeeded` with pull request evidence
has its merge armed through the GitHub auto-merge mutation with the rebase
method, the equivalent of `gh pr merge --auto --rebase`, and the lane audits
`merge_armed`. Exactly one factory pull request is armed at a time, because
this repository merges through the GitHub merge queue and an ejection cascades
across every candidate behind the one that failed. The holder is read from the
lane's own audits and from GitHub: before arming anything, one page of open
pull requests is listed and any pull request on a `factory/` branch with
auto-merge already set counts, so a pull request an operator armed by hand is
not raced. A holder check that cannot be read arms nothing, because not knowing
is not a licence. Every waiting delivery audits `merge_deferred`, once per
blocking pull request, and is armed on a later tick.

Selection is on landing state, never on recency: every non-advisory `succeeded`
receipt whose landing has not reached a terminal audit, oldest first. Terminal
is `merge_arm_refused`, which hands the pull request to a human, or
`issue_closed`, which is the last step of a successful landing. Taking the
newest receipts of any class instead let a burst of advisory settlements push an
armed but unmerged delivery out of the batch, which left it never observed and
the holder reading as absent. A delivery the lane has never touched is skipped
once it is a week old, so turning the flag on does not stampede over history,
but anything the lane has armed is followed to a terminal state whatever its
age.

Later ticks observe the armed pull request:

| What GitHub shows | What landing does |
|---|---|
| merged | audits `merged`, then closes the issue |
| closed, not merged | audits `merge_arm_refused` and stops |
| open, head moved off the armed SHA | turns auto-merge back off, audits `merge_arm_refused` with `head_moved` |
| open, auto-merge gone | audits `merge_ejected` and re-arms |
| open, still armed | waits |

An open pull request whose auto-merge GitHub has turned off was ejected from the
merge queue. Watching only for a closed pull request wedged the holder forever,
so the ejection is named and the pull request is armed again: the queue analysis
failure class is usually transient. After two ejections the lane audits
`merge_arm_refused`, sends one Discord warning, and leaves the pull request for
a human, because an invalid merge commit needs a rebase no node here can do. The
head recheck is what stops a branch that moved under an armed pull request from
merging something no reviewer approved.

A mutation GitHub refuses audits `merge_arm_refused` and is not retried; a
GitHub read or write that fails audits `landing_error` by exception type and
status, never by response body, at most once an hour per task, and the next tick
retries. `merge_armed` and `merge_ejected` are counted rather than fenced,
because a delivery can be armed, ejected and armed again; every other step
writes one row per task and that row is its fence.

Landing stops at the merge. Confirming that the chart version write-back landed
and that the new image is live is the verify node #6002 phase 4 still owes; the
`merged` audit carries a `rollout_verified` field that is null until that node
exists.

## Controls and uncertainty

`pause_admissions` allows already admitted work to finish. `pause_task` stops
new nodes for that task. `stop` durably fences factory admission and descendants,
including the shared pending-message sweep and transport creation/invoke retries.
The coordinator makes at most two recorded cancellation attempts per active node.
Other operator-owned sessions are outside this control scope.

A network operation already in flight can remain uncertain after stop. Status
continues to show those reservations and cancellation requests; the stop flag
does not assert that every guest has ceased. Stop is terminal for this first
bounded lane. It cannot be reset by replaying an earlier enable request.

Agent workloads have a twelve-hour runtime backstop. The caller's result wait
and routine drainer observation wait exceed that ceiling. The CLI silence
backstop is eleven hours and fifty-five minutes: a quiet build is not by itself
proof of a stuck task. DAG node limits remain explicit immutable policy;
raising the runtime ceiling does not rewrite admitted attempts or grant retries.
Fresh sessions have a one-day lifetime, and an older session has only the time
remaining before its absolute expiry. Idle parked sessions still expire after
one hour.

The current stop path fences admission and requests DBOS workflow cancellation.
It does not yet guarantee termination of an external Ember guest. Issue #5922
tracks the bridge to the exact owned invocation, cessation confirmation, ongoing
run inspection and bounded resume. Until that is implemented, an uncertain
attempt retains its capacity and cannot be restarted merely because its observer
or workflow timed out.

## Validation

New tests have explicit targets in `projects/monolith/BUILD`. File-backed
hermetic tests cover concurrent receipt deduplication and admission, immutable
policy and pins, rollback across both reservation ledgers, task and node limits,
latest-review evidence, and the complete issue/planner/work/review/PR sequence.
Transport tests cover stop during capacity retries and independent concurrent
contexts. Linux orchestrator CI and a real bounded operating trial remain the
delivery gates; local tests are advisory.
