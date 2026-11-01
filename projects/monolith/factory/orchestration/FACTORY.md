# Bounded issue delivery lane

## DBOS recovery needs process cessation

Issue #6798's accepted investigation reproduced a DBOS 2.29.0 duplicate-step
conflict with two processes sharing a workflow identity. The recovered executor
abandons its workflow body and waits for the terminal result. If the original
executor then exits, native session completion cannot resume that waiter. A
third process recovery resumes the original workflow. The SQLite reproduction
confirms this mechanism; the October 3, 2026 production incident's conflicting
checkpoint and original executor were not observed, so historical attribution
remains inconclusive.

**Why.** DBOS `destroy()` shuts down its synchronous executor with `wait=False`.
Running workflow threads can survive destruction and race recovery after an
in-process relaunch. Teardown reordering alone also leaves the five-second lease
shorter than the native executor's fifteen-second rollout drain. A launched
factory therefore exits the process after orderly shutdown instead of destroying
and relaunching DBOS in that process. The framework leaves election running and
adds an independent shutdown renewal thread. A renewal watchdog fails closed
before the lease can become stale, and a twenty-second absolute watchdog bounds
cleanup within the thirty-second termination grace. The lease expires after
process cessation; it is never explicitly released while DBOS threads survive.

Loss of leadership or failed singleton startup with launched DBOS exits promptly
without draining under assumed ownership. Other binaries retain normal election
and release behavior. Normal rollout still fences admission on SIGTERM, drains
native executor handoffs under #5938, and runs module shutdown hooks. Unknown
native invocation outcomes and operator pauses remain fenced. Node workflow
member sources and the derived application version are unchanged.

The CI-wired `leader_recovery_test.py` uses real DBOS processes and the unchanged
`execute_node` workflow. It covers the duplicate-result race, persisted polling
sleep recovery followed by a late native result and an existing plan artifact,
one dispatch per original attempt/session, live waiting, unknown invocation,
and an operator pause retained across recovery. Native I/O uses isolated file
seams. Live acceptance still requires a future GitOps rollout with node and drain
checkpoints continuing past their recorded sleep wake times. The original
sessions 17053 and 17055 settled SUCCESS on October 3 at 05:10 UTC and must never
be retried or recreated.

## Domain ownership

Factory is one domain inside the monolith. It owns private interactions and
MCP, published public views, task orchestration, execution, budgets, and recovery.
Shared authentication verifies caller identity. Factory orchestration controls
require a standing human in `operators` through `factory.access.is_operator`.
Browser decisions use the single Envoy-projected `X-Auth-Email` claim, optionally
narrowed by `FACTORY_OPERATOR_EMAILS`; an unverified Cloudflare email header
grants no authority. Per-record caller scoping remains incomplete on other
surfaces, including task tools and session reads and mutations, as well as note
lookup and turn-detail reads. These examples are not an exhaustive gap list.
See [the architecture surface matrix](../../ARCHITECTURE.md#7-mcp-surface)
for the authentication and result-access boundaries. Public composition loads
only the read-only factory descriptor and published projections.

The private registry composes `factory.module` once. Its lifecycle owns both
the conductor/DBOS runtime and session maintenance, including partial-start
cleanup and the process watchdog. Implementations live in `factory.orchestration`
and `factory.execution`. The old `swarm` and `agent_sessions` packages contain
compatibility shims for durable exception identities and the existing Discord
adapter. Database schemas and durable workflow identities remain stable.

The private interface starts at `/factory`, with decisions at
`/factory/escalations` and run, session, and voice details at
`/factory/execution`. The launcher presents one Factory entry. Legacy `/agents`
links and browser API requests resolve internally to the corresponding factory
routes on the private host, preserving query parameters and request methods.
The backend `/api/agents` contracts remain stable. Both legacy and canonical
prefixed VM stream paths retain the gateway's 600-second timeout.

The public reader ships `factory.public_view` and excludes private execution,
orchestration, access, and projection modules. It reads only published public API
views and snapshots with the existing restricted database role.

Discord integration is outside this consolidation. Existing integration behavior
is preserved pending an explicit retirement or redesign. This change introduces
no general-purpose cross-domain factory API or separate factory deployment.


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

### Sessions and legacy workflows

`agent_sessions` persists sessions, turns, pending messages, progress, model
selection, workflow ownership, and EmberVM lineage in Postgres. Pending turns
are claimed in sequence by one replica, refreshed by heartbeat, and reclaimed
after a stale lease so a crashed worker does not strand work. A turn whose
brick is preempted mid-flight is resumed or re-issued rather than lost, and the
model the guest actually ran is persisted on the turn.
(see: /projects/monolith/factory/execution/models.py)
(see: /projects/monolith/factory/execution/store.py)
(see: /projects/monolith/chart/migrations/20260903030000_agent_session_recovery.sql)

Model aliases and runtime families come from `factory.execution.model_family`.
`spark` selects Muse, `pi-spark` selects Pi, `luna`, `terra`, `sol` and `astra`
select Codex, and `opus`, `sonnet` and `fable` select Claude. Persisted `qwen`
normalizes to `spark`. The production picker narrows `SUPPORTED_MODELS` with
the chart's model allowlist. Claude, Codex and Muse use `claude-runtime`; Pi
uses its separately configured workload. Session guests and disposable
language-specific `run_code` workloads are vsock-only with no NIC.
(see: /projects/monolith/factory/execution/__init__.py)
(see: /projects/monolith/factory/execution/transport.py)
(see: /projects/monolith/sandbox/mcp.py)

Legacy swarm composes sessions through `implement_then_review`: a Luna implementer session, an Opus
review session, bounded attempts and review cycles, and a plan pinned into a
step record at start so replay can never see a budget move. The pinned
`budget_usd` is enforced at node boundaries. Every structured output an agent
makes, review verdict and rationale record alike, arrives through one typed
artifact channel validated at ingestion; nothing structured is parsed out of
prose. For a legacy swarm run the workflow's Python control flow is still the
graph, so the orchestration-level mutable DAG that ADR agents/062 decided
reaches those runs only when this engine is retired (#5419, #4781).
(see: /projects/monolith/factory/orchestration/workflows.py)
(see: /projects/monolith/factory/orchestration/budget.py)
(see: /projects/monolith/factory/orchestration/turn_artifact.py)

At each legacy swarm escalation the workflow
writes a `swarm.swarm_decision` row and polls it on the cadence it already
polls session turns; a human answers through the console, the run decision
endpoint, or the `agent_run_decide` MCP tool, with the actor recorded
best-effort the way `cancel_run` records one. An unanswered row expires after
`swarm.decisionTimeoutSeconds` and the run ends the way an unwatched gate
always did. Legacy `implement_then_review` stops short of merging: its role-separated
review gate from ADR agents/027 is unimplemented. Its separate docfix path uses auto-merge,
off by default and refusing agent instructions, skills, runbooks and ADRs even
when on.
(see: /projects/monolith/chart/migrations/20260822230000_swarm_decision.sql)
(see: /projects/monolith/factory/orchestration/router.py)
(see: /projects/monolith/deploy/values.yaml)

The work-queue drainer turns a standing backlog into continuous progress. An
Argo tick every fifteen minutes asks the leader to start `drain_cycle`, a
leader-owned DBOS workflow that claims `claude_agent.routine_jobs` rows under
the existing skip-locked lease, runs each as one fresh Luna session, and
completes it. Two job kinds are claimed: the general lane (`qwen-drain`, a
legacy name kept so registered rows stay claimable) and knowledge extraction
(`kg-drain`, capped per day with manual burst grants). Draining is serial by
design, the tick's suspend and the `agents.drainer.enabled` value are the two
kill switches, and a stall advisory rather than a Discord notify is the default
failure surface.
(see: /projects/monolith/factory/orchestration/drainer.py)
(see: /projects/monolith/agent/routine_jobs.py)
(see: /projects/monolith/chart/values.yaml)

The console lives at `/factory/execution`, with the board at `/factory` and
decisions at `/factory/escalations`. Its run view presents the plan, decisions
and walkthrough. The extraction queue shows drainer backlog, the VM view reads
the control plane and stays empty when unavailable, and the voice companion
is ledger-first. Output reaches Discord when a turn needs a person, unless the
session is bound to a thread. The read-only `GET /api/agents/factory` join
combines status, graph, runs and session summaries, separately from the
operator-gated `/api/swarm/factory` controls. Discussion opens an ordinary
session with the task plan; it creates no second chat surface.
(see: /projects/monolith/factory/private_view.py)
(see: /projects/monolith/frontend/src/routes/private/factory/+page.svelte)
(see: /projects/monolith/frontend/src/routes/private/factory/execution/+page.svelte)

### Cadence and singleton ownership

Monolith batch work is rendered as Argo CronWorkflows in the workflows
namespace, whose controller owns cadence, concurrency, deadlines, and history.
Each entry runs the digest-pinned jobs image with one `jobs_main.py`
subcommand. A job pod gets `DATABASE_URL` from a Kyverno-cloned Secret plus
whatever the entry declares, never the deployment's environment. Entries marked
`internalApi` only POST a private endpoint on the leader (the drain tick and
the synthetic probes), so that work runs inside the API pod
where the credentials already live. Suspended entries remain available for
manual submission.
(see: /projects/monolith/chart/templates/cronworkflows.yaml)
(see: /projects/monolith/app/jobs_main.py)

The scheduler API retains `scheduler.scheduled_jobs` and its `ScheduledJob`
model for list/get and legacy run-now names. The chart also puts every
`replaces` name in `ARGO_JOBS`, including suspended manual-only entries, and
uses the same annotation to locate the CronWorkflow for submission. The dormant
in-process registry, registration hooks and registry-based orphan check are
removed under #7021.

Leader election scopes side-effecting singleton hooks to one API replica: the
Discord bot, outbox drain and message-lock sweep, AIS ingest, the agent-session
loops (pending-message sweep, title refresh, Ember-session export into the
knowledge graph), the DBOS runtime, and the continuous-delivery probe writer. A
failing singleton resigns and retries rather than ending leader election, and a
failed acquire backs off while followers keep serving.
(see: /projects/monolith/framework/core.py)
(see: /projects/monolith/chat/leader.py)
(see: /projects/monolith/factory/module.py)

## Conductor, Planner, and Executor

The **Factory** deploys and orchestrates agents on Ember to produce features
and address issues. The **Conductor** is its operator-facing manager. It
receives goals and steering, maintains a prioritized view of work and bounded
task context, selects work, delegates each selected task to the Planner, and
reports outcomes with evidence. A Conductor instruction proposes work through
the existing factory interfaces. It does not bypass admission, controls, or
the authority of the record being changed.

The **Planner** is the Astra-selected per-task planning role. It receives one
selected task, its acceptance and operator constraints, current factory state,
and permitted task-relevant KG knowledge. It creates or amends that task's DAG.
Existing `factory_conductor`, `task conductor`, `conductor_model`,
`SwarmConductorCall`, and `conductor_<n>` names are legacy names for this
Planner boundary. They are retained to keep durable and code identities stable,
not because the Planner is the operator-facing Conductor.

The **Executor** is the existing graph engine and dispatch path. It runs
accepted DAG nodes for the existing `implement_`, `investigate_`, `review_`,
`refine_`, `integrate_`, and legacy `conductor_` role prefixes. Those workloads
keep their existing principals, profiles, and server-enforced authorization.
Conductor, Planner, Executor, and node role names describe responsibility only.
No role name grants permission.

See [factory evolution](#factory-evolution-follows-delivery-authority) for the
Conductor's improvement mandate and its delivery authority boundaries.

### Authoritative records and context

The roles use the existing owners rather than a second scheduler or approval
ledger:

- `swarm.work_item` and its append-only `swarm.work_item_event` history record
  the durable unit of requested work and its source-owned state. A
  `swarm.factory_receipt` captures submission, queue/admission state, its work
  item and issue identity, and the admitted `swarm_task` link.
- Queue order is the eligible `factory_receipt` order `(created_at, id)` after
  lane, policy, active-work, and blocker checks in `admit_next`. There is no
  separate priority field for the Conductor to rewrite. Priority rationale may
  be retained as context, but changing live order must use an existing
  authorized task or control operation. KG text is never queue state.
- `swarm.swarm_task`, `swarm.swarm_plan_version`, `swarm.swarm_plan_node`, and
  `swarm.swarm_node_run` are the task, accepted DAG history, nodes, and execution
  evidence. The Planner proposes graph edits and the server records accepted
  edits; the Executor dispatches only recorded nodes.
- A receipt's `escalation_json` is the current pending or resolved factory
  decision, and `direction_json` is the approved direction supplied to a
  re-admitted Planner. The append-only `swarm.factory_audit` rows retain
  decision claims, requests, results, and control acknowledgements. A model
  suggestion is not an approved decision.
- The singleton `swarm.factory_control` row is the authoritative factory state,
  policy, and version. Per-task pause and cancellation state remains on the
  `factory_receipt`. Every mutation revalidates current records and authority.

Issue #5787 owns maintenance and scoped retrieval of Conductor knowledge.
Committed operator exchanges enter the existing KG ingestion and extraction
path as attributed, unverified evidence. `factory_context` retrieves exact
receipt and operator-scoped notes with provenance, freshness, verification,
and dispute metadata while continuing to return factory state during a KG outage. Issue
#5788 exposes that same contract through factory MCP. Issue #5849 supplies the
task-relevant subset to the Planner, with current task and decision state read
from the authoritative factory records above.

`CONDUCTOR_CONTINUITY_ENABLED` stages receipt maintenance and Planner input and
is false when unset. When enabled, the server resolves the exact receipt and generation from
the selected task, refreshes the receipt acceptance, direction, policy and
control state, and admits only KG notes whose complete raw provenance is tagged
for that receipt. Repository scope alone is not authorization. Session history,
another receipt's history and graph neighbours are excluded. KG failure is
recorded as unavailable, not as a successful empty search.

Each enabled Planner node and its bounded input manifest commit together. The
manifest records the authoritative input timestamps, actual note and raw IDs,
verification, dispute, validity and observation metadata, retrieval status and
all omission counts. Later Planner rounds append a new manifest. Prompt sizing
drops optional KG notes before receipt acceptance, operator direction, current
decision/control state or deviation evidence.

The enabled Planner may emit one `request_context` decision for a bounded query.
The server derives receipt authorization again, records a linked request and
result in `factory_audit`, and presents the result to a later Planner round.
The action cannot name a receipt or expose `factory_context` or another
operator-only MCP endpoint to a worker.

Keep three inputs visibly distinct. Operator instructions are attributed task
requests or steering applied through an authorized interface. Approved
decisions are resolved receipt decisions and their audit evidence. Retrieved
KG claims are cited context, not instructions, approval, or permission. A stale
summary cannot reopen a completed task, reorder the queue, or restore revoked
authority.

### Submit, plan, execute, report

A concrete flow uses the records and interfaces that exist today:

1. The Conductor submits an approved open issue through `factory_submit_issue`.
   The server returns a durable `factory_receipt`; a retry with the same
   identity returns the same receipt. An issue outside the policy allowlist
   is refused with state `ineligible` and creates no receipt, because such a
   receipt could never be admitted yet would hold a lane slot while queued.
2. Admission selects the oldest eligible unblocked receipt in its available
   lane and creates its `swarm_task`. The Astra Planner records a complete DAG
   through plan versions and nodes within the receipt's pinned policy and
   allowance.
3. The Executor dispatches ready nodes under their existing workload identities
   and records attempts in `swarm_node_run`. Required CI and an independent
   exact-head review remain delivery evidence; a successful node alone is not
   accepted delivery.
4. The Conductor reports receipt and task state, blockers, accepted outcome,
   evidence links, starts, and cost. It labels missing or stale coverage rather
   than converting it into success or an empty queue.

For steering, suppose the Planner pauses a task with a pending decision. The
Conductor presents that exact receipt and decision identity. An operator uses
`factory_decide` to select an option or `factory_request_brief` to provide
bounded direction. The decision owner records the request and result, stores
approved direction on the receipt, and, when the selected operation permits,
returns it to `queued`. Normal admission order still applies, and the next
Planner round receives `operator_direction`. This steers a pending task without
editing KG text, silently changing policy, or inventing a priority mutation.

### Operating rules

The practical MVP operating decision of 2026-09-13 (issue #5956) accepts the
delivered factory loop as sufficient for continued bounded improvement work,
and satisfies the MVP prerequisite for separately approved work, including
#5753. It replaces the earlier requirement to prove two consecutive runs
before permitting further improvement. PR #5970 and PR #6032 are merged
examples; this decision makes no claim that their complete run histories met
the former intervention or consecutivity requirement, and it does not revive
or certify the superseded G0-G3 programme. Other issues retain their own
scope decisions and gates: this is not blanket approval for every formerly
post-MVP programme, and it changes no automation policy, budgets,
concurrency, merge authority, or deployment settings. No conflicting
MVP-gate language remains in this file; earlier checkpoints live on in GitHub
edit history and discussion and are not current acceptance requirements.

It retains four rules for all of these roles:

1. Do not add frozen packets, hash pinning, or timed approval windows. A PR is
   accepted by required CI plus one independent exact-head review, as for a
   human PR.
2. Report one line per task: outcome, blocker, next action, starts, and USD
   used. Do not narrate what did not happen.
3. Cost per accepted PR is the metric. Record it on the PR when it lands.
4. Planner turns share the task's start budget with implementation. If planning
   consumes more than half of the starts, stop the task and respec it by hand.

### Planner prompt layout

A planner node's guest prompt is built static-first. It opens with the output
contract and decision schema from `node_workflows._node_prompt`, then
`_PLANNER_CHARTER` in `factory_conductor.py`: the planner boundary, the gate
policy and the charter, identical for every task, round and attempt. A labelled,
server-authored task section follows with the task id, repository, branch, base,
closing keyword rule, conductor gate guidance, and the flag-dependent funding,
budget and judgment-class rules. The charter scopes untrusted data to the JSON
context (task text included) and tool results, so these server rules keep their
authority although they sit after it. Then comes the JSON context, serialized by
`_planner_json` in a fixed stable-first key order (`PLANNER_CONTEXT_ORDER`,
nested keys sorted), and last the attempt's working branch, prior-attempt
evidence and artifact path. The size cap and its shrink loop only change the
JSON, which sits after the static block. Knowledge graph recall for the task
goes at the very end of that first user message, after a blank line
(`mcp._factory_first_message`), and never into the session's system prompt:
every factory node session gets the same system prompt, the shim's sandbox
prompt with nothing per task appended. Interactive and launcher sessions still
carry recall in their system prompt.

**Why.** Every planner round is a fresh guest session with no resume, so the
only prompt the provider can reuse is the byte-identical start of the text.
Claude Code caches a prefix automatically (five-minute TTL, 512-token minimum)
and Codex caches prefixes the same way, but the old layout opened with
"Factory task {id}, repository ..." and put the schema last, so almost nothing
matched between rounds. The system prompt renders before the user message
(`--append-system-prompt` on Claude, `developerInstructions` on Codex), so a
per-task recall block there would end the shared prefix before the charter;
recall moved to the tail of the user message for that reason. Putting the
static charter and schema first and the volatile context and recall last makes
that prefix cacheable across rounds and tasks. Whether it is actually read from
cache is measured, not assumed: `cache_read_tokens` in `AgentTurn.usage_json`
on planner turns gives the hit rate. The Claude Code CLI also puts its own
environment section (date, git status) in its system prompt, ahead of the
charter, so cross-round hits on Claude depend on that section staying the same
too.

**Charter content.** The charter states authority, untrusted data, the
outcome and exit criteria, and the defaults the server cannot infer. Mechanics
the server enforces (reserved keys, cost clamping, turn arithmetic, exception
field minimums) live in the schema, refusal codes and `decision_feedback`.
Restore a removed line only after a repeated planner failure that the feedback
did not prevent. The cut from about 1,800 to about 560 words is
measured by planner refusal rate, planner rounds per task, first-pass review
rejection and planner input tokens against the prior 14 days.

### Planner preview

The proposed-decision preview is staged behind
`swarm.factoryPlannerPreviewEnabled=false`, which renders
`FACTORY_PLANNER_PREVIEW_ENABLED=false`. `planner_preview.py` is an unregistered
server-side read adapter over the shared submission projection. It adds no
HTTP route, MCP tool or guest catalogue entry. The private MCP surface serves
standing humans and the pruned agents tier lacks delegated task identity
verification (`projects/mcp/ARCHITECTURE.md`, `docs/security.md`). The production
binding verifier therefore returns no binding even with the flag on. Operator
tools keep their existing `_refuse` and `factory.access.is_operator` floor.

The adapter takes a schema-valid `plan` or `add_node` decision, using the real
decision/edit schema and bounds, plus an exact integer `expected_revision`.
It resolves identity from the verified request principal and a server binding
verifier, never from caller task, receipt or node IDs. A delegated workload
binding must name a non-terminal `conductor_<n>` run of that same admitted,
unpaused, uncancelled task. Missing or ambiguous bindings refuse. The database
checks the run and task before evaluation. A unique insert into
`swarm.factory_planner_preview` reserves ordinal 1 or 2 for that run. Every
enabled, bound, schema-valid call reaching the revision check consumes a slot,
including stale revisions, refused projections and retries. A third call
refuses `preview_limit_reached` without evaluating. This ledger is the preview's
only write: graph versions, starts, receipts, allowance/refusal audits, funding
overlays and decisions stay unchanged. Results are advisory; submission repeats
current-state validation and accounting. The enabled prompt asks for at most two
previews, then a feasible smaller decision, authorized funding request or pause,
with measured needed/allowed turns and USD and reserve-aware spare figures.
Repository delivery leaves the flag off. #6650 is closed completed; its recorded
staged acceptance calls for binding provisioning, authorized enablement with a
live in-turn preview, and a 72-hour rejected-run/cost comparison. Repository
completion does not establish those operational observations. Its reported
baseline for 2026-09-28 through 2026-10-01 is 11 `envelope_exceeded` refusals and
$10.17; savings count only eliminated rejection or replan work.

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
is what an accepted plan has to fit inside; `task_budget_usd` is recorded for
accounting and never refuses a plan (#7161). The number of
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
and `intake` are optional. `max_review_recovery_rounds` is also optional and
defaults to 0; it admits up to two extra rounds under the CI gate below.

### Serving more than one repository

The optional `repos` map keys per-repo policy by normalized repo slug. Each
entry holds its own enable switch, pause switch, intake labels, lane caps
(`max_tasks` plus `max_per_day`), budget envelope (`task_budget_usd` and
`turn_budget_usd`), landing mode (`merge_queue` or `none`, with `auto_merge`
off unless explicitly set), and charter reference. A policy with only the
legacy `repo` field normalizes to a one-entry map that behaves exactly as
today, so the live policy needs no re-post. Global pause, stop and generation
stay global. Per-repo pause, resume, enable and disable are separate control
actions that flip one entry without advancing generation or retiring any
other repo's queue. Admission checks lane room, the daily cap and the budget
envelope against the receipt's own repo. Any entry that is not the legacy
primary repo defaults to disabled.

Entries are stored sparse: an entry holds only the keys an operator set, and
every other key is read from the global block (`intake`, `max_tasks`,
`task_budget_usd`, `turn_budget_usd`, `auto_merge`) at the moment it is
used. Raising a global value, through `configure` or a direct JSON merge,
therefore reaches every repo that did not override it, and the legacy
primary never carries a frozen copy. Landing serves only the primary repo:
a delivery on any other repo settles at ready for review, whatever its
entry says about `auto_merge`. In the intake sweep a repo at its daily cap
is skipped while each lane picks its candidate, so a capped repo never takes
the delivery slot from another repo with room.

The staged `weave-hand/loom` entry ships default-off. Enabling it is a
separate operator act: post the payload from
`migration_policy_with_loom` (staged daily cap 2, landing `none`, loom
charter) through `configure`, then flip the entry on when the loom charter,
sweep and review path land. No live enablement happens in the change that
adds the map.

The webhook and the intake sweep follow the same map. A delivery for a repo
outside the enabled entries is ignored with no work item and no error; the
legacy `FACTORY_GITHUB_WEBHOOK_REPOSITORY` value keeps working as the
single configured repo, and `FACTORY_GITHUB_WEBHOOK_REPOSITORIES`
(`swarm.factoryGithubWebhookRepositories`, empty by default) names any
extras. Trust rules apply per repo against the same configured authors.
The sweep reads every enabled repo under that entry's own intake labels and
daily cap, and each planner and review prompt carries its repo's charter:
homelab receipts read the existing charter unchanged, loom receipts read
the loom fragment (its GitHub Actions workflow with buck2, no merge queue,
`auto_merge` off).

Staged loom enablement, in order: set
`swarm.factoryGithubWebhookRepositories` to `weave-hand/loom`, post the
`migration_policy_with_loom` payload through `configure`, confirm the loom
entry is present and disabled, then enable it. Adding the entry changes the
policy, so `configure` refuses it with `generation_not_advanced` while any
task is admitted or uncertain, and advancing the generation instead retires
homelab's queued receipts. Post the payload while the lane is idle. Operational acceptance stays
on the issue: enable with `max_per_day: 2`, submit one loom ready issue,
and confirm a PR opens on loom with green CI and an independent review
artifact.

### Updating policy while work runs

`configure` accepts a new policy while tasks are admitted or uncertain. Each
running task keeps its pinned policy, dispatch history, budget and deadline;
only future admissions take the new policy. While tasks are active, a changed
policy must supply a generation newer than the current control policy. An
identical retry is accepted without another generation change. An accepted
generation advance cancels queued and escalated receipts from older
generations in the same control-row transaction that publishes the new policy.
Each retirement is audited with both generations and a reason. The receipt is
never re-stamped, and admitted or uncertain work keeps running under its pinned
old policy. Unrelated terminal history is left unchanged.

An unresolved escalation on a retired receipt is dismissed locally in that
same transaction, with a resolution explaining the generation change. The
paced escalation reconciler repairs preexisting old-generation queues and
cards in bounded groups of 50, including cards attached to terminal receipts;
it resolves those cards without rewriting their terminal receipt state or
touching GitHub. Repeated passes are idempotent. A claimed decision temporarily
refuses the generation change, so policy retirement cannot race an external
issue mutation. Conversely, a decision request whose receipt no longer matches
the policy generation is refused before labels, comments, child issues, or
issue state can change.

That refusal is for retired cards. With `FACTORY_ACTIVE_RECEIPT_DECISIONS_ENABLED`
(`swarm.factoryActiveReceiptDecisionsEnabled`), a card whose receipt is still
`admitted`, `uncertain` or `landing` is decidable whatever the current
generation, because a generation advance never retires active work: the task
keeps running under its pinned policy, and its card (a no-progress watchdog
pause, most often) is a live question about that task. The existing rule that
a running receipt accepts only its watchdog decision or the local stop on its
automatic-pause card still applies. A settled
or queued receipt on an old generation is refused exactly as before.

**Why.** On 2026-10-01 receipts 700 and 703 were still `admitted` on
generation 13 when the policy moved to 14, and `factory_decide` refused their
watchdog cards as belonging to an old generation. The only way out was
`resume_task` by hand.

Configuration preserves control state: enabled work continues, paused
admissions stay paused, and initial configuration still needs `enable`.
`stop` remains irreversible. The configure audit records
`active_tasks_on_previous_policy` so the transition accounts for work left
running under its old pins.

Active tasks from every generation count against lane and chart capacity,
including on the board. Lowering a limit waits for that work to finish rather
than stopping it. Admission cannot start another task on the same repository
and issue while an older task is active, even in another lane; autonomous
intake skips such issues with `active_issue` evidence. Other eligible queued
work can still fill the available capacity.

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

Delivery review prefers Opus on the shared Claude subscription, and that
window is one input the factory can exhaust:
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

The planner uses that dispatch evidence too. A permitted fallback does not
require another review merely to obtain Opus provenance. The factory approval
is the successful independent review run's structured artifact at the current
PR head. A posted GitHub approval is a separate requirement only when an actual
repository rule or explicit task acceptance requires it. Explicit model-specific
acceptance criteria and the judgment capability floor still apply.

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
`reviewer_fallback` and `reviewer_restored`; changed quota readings and a
five-minute evidence refresh also update an existing routing choice. The board renders
the state from that ledger without a broker call. An unknown reading, or one
older than an hour, never starts a fallback: it audits `quota_guard_unknown` at
most hourly during a continuing outage, records each new outage after recovery,
and review stays where it is, because downgrading every review
whenever a broker read fails would turn one outage into two. It does not end a
fallback either, so a fallback entered at 95 percent does not snap back to Opus
the moment the broker goes down. A known weekly reset releases that window's
high-usage latch even if the broker subsequently loses its observation. The
reset is carried through waiting/fallback transitions. Legacy records without
a known reset remain conservative rather than guessing a reset date.

The board never presents the old decision's percentage as current after a newer
unknown observation or more than an hour of age. It names retained fallback
routing with unavailable quota instead. A current percentage is labelled as
usage, separately from the fallback threshold.

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
| `judge` | funding decisions and reservation-lease reviews | `["opus"]` when allowed, else the `reviewer` pool |

A role pool must start with that role's own configured model, so the policy's
stated preference is what the pool is ranked from. A class pool, `implement`,
`refine`, `reviewer` and `judge`, names no policy field and so has no head to anchor to; its order
is the preference. Every member of every pool must appear in `allowed_models`.

A GitOps policy change is a one-shot guarded migration that bumps `version`
and writes a `swarm.factory_audit` row (for example
`20260924060000_factory_policy_no_astra.sql`); the operator control surface
remains the normal path for changing policy.
`20261017070000_factory_policy_restore_astra.sql` restores Astra to planning
and advisory refinement for the generation-14 Opus/Sol policy (#6983). It adds
Astra to implementation and reviewer pools while Sol and Opus retain those
roles' first preference. The temporary September quota-saving exclusion had
outlived its reset; both Codex grants had headroom on 2026-10-17. It preserves
all task bounds, quota floors and pinned receipts, and audits the policy change.
An operator-changed role or generation, an existing Astra allowance, or a full
affected pool makes the migration a no-op. Verify the live policy after rollout.
Rollback uses the normal policy control to restore the audited prior routing;
do not rerun or rewrite the historical migration. Existing tasks keep their
pinned model choices in either direction.

`20261017100000_factory_policy_astra_planner.sql` handles the live policy that
070000 skipped because Astra was already allowed as a reviewer fallback. It
puts Astra first for planning and refinement, appends it to implementation
pools when absent, and preserves the existing Astra reviewer pool. Pinned
receipts and all bounds stay untouched. Rollback uses the normal operator
policy control to restore the audited prior routing; never rerun or rewrite
a migration.

`20260930060100_factory_policy_claude_55.sql` stages `sonnet` (pinned to
`claude-sonnet-5-5`) behind `spark` in existing worker and implement pools and
raises `task_budget_usd` to at least $50, without moving `worker_model`.
`20260930210000_factory_policy_gen14_sol.sql` advances `generation` from 13
to 14, so issues that already failed at 13 become eligible for intake again,
and moves `worker_model` to `sol`, putting `sol` first in the worker,
implement and refine pools so delivery spends Codex quota before Muse and
Claude.

### Reasoning effort by role and node

Every dispatch pins reasoning effort chosen by the node's role, beside
the model it settles on (#6461). Planner (`conductor_*`) and review nodes run
at `xhigh`; funding judges (`conductor_funding_*`), implement, integrate and
correct nodes at `high`; investigate and refine nodes at `medium`; any other
role at `high`. The optional policy field `role_effort` overrides any of
`planner`, `reviewer`, `funding`, `implement`, `integrate`, `correct`,
`investigate` and `refine` with one of `low`, `medium`, `high`, `xhigh` or
`max`. The guest applies effort to Claude and Codex turns and records it on the
turn with `provider_model`, the model that actually ran behind the alias; a
session without an effort gets the guest's per-model default (`opus` and
`fable` `high`, `sonnet` `medium`). Because xhigh costs more, a Claude
planner node reserves 1.5x `turn_budget_usd` and the Opus review floor is $10.

The Astra coding policy (#6992) makes new workers and implementers prefer Astra,
with `implement` at `medium`, and `planner`, `reviewer`, `integrate`, and
`correct` at `xhigh`. Opus remains the independent reviewer. A plan edit may
set `effort` to raise the role floor, for example `xhigh` before implementing a
migration, concurrency fix, security change, architectural change or difficult
bug fix. The graph persists that choice and each dispatch pins its effective
value. Failed Astra attempts stay on Astra at least at `xhigh`; fallback pool
order never turns an Astra failure into a downgrade to Sol. Capacity denials
and proven no-model starts do not trigger capability escalation. Existing
receipt policies and admitted pins are not rewritten, and callers omitting
effort retain the guest's per-model defaults.

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

#### Escalation up the implementation pools

Quota decides where an implementation node starts; outcomes decide where its
next attempt runs. When an `implement_`, `integrate_`, `correct_` or
`investigate_` attempt failed, or a correction answers a review that returned
`changes_requested`, dispatch runs the next attempt on the next member of the
node's pool (`implement`, or `worker` for investigation) after the model that
did the work, skipping members that `availability()` walls. The node keeps
its planned model; the attempt's pin carries the new `model` plus
`escalated_from` and `escalation_reason` (`failed` or `changes_requested`), and
an attempt that already climbed stays on its rung when the pool is spent. The
failed model is reused, as before, when it is the last member, when it is not
in the pool at all (an explicit planner pin outside it), or when every member
above it is walled. Judgment classes never escalate: they stay on the judgment
floor. An escalated attempt is admitted and authorized against its new model,
reserving the node's full `max_cost_usd` for accounting. Every `swarm.swarm_node_run` row
records the dispatched model in its `model` column.

**Why.** Implementation starts on the cheapest capable model (Muse, `spark`)
and escalates one pool step per failed attempt or changes-requested review,
rather than handing the same model the same problem again: a retry on the
model that just failed spends a turn on the outcome most likely to repeat,
while starting every task on the strongest model spends it on work the
cheapest one could have done. The per-run `model` column exists so escalation
outcomes by task class can be measured, which feeds #6283's decision on
whether routing should learn from that history.

### Execution capacity and release intent

Execution capacity uses one projection: admitted and uncertain receipts count
across generations, except paused or landing-held receipts with no reserved or
uncertain starts. Each database caller supplies that accounting evidence in one
batch; missing evidence retains the slot. Current admission-eligible queues
reserve intake room. Per-repository lane caps count the same execution-slot
holders. Waiting and landing receipts retain issue and branch ownership.

Authorized resumes and landing-hold releases retain their fences and a durable
release request while their repository lane, global lane or chart-wide ceiling
is full. The regular reconciler and admission retry those requests under the
singleton control lock.
A new pause, including a runtime-meter fence, cancellation, human handoff or
replacement hold revokes pending release intent, even if the receipt was already
paused. Settlement other than cancellation or human handoff applies a surviving,
still-matching hold removal without an execution slot regardless of a pre-existing
pause, and revokes only the resume part. Both settlement and capacity-retry audits
retain the landing authorization so same-key replay acknowledges an applied hold
release. A revoked request retains its original acknowledgement on replay and
cannot recreate release intent.
Revocations record the request and reason in the audit.
A hold-only release on a paused receipt preserves the pause and needs no
free slot. No request means no automatic resume. Held repository work reacquires
capacity at its next start reservation. Completed decisions and settlement need
no execution slot and still reconcile while capacity is full. A capacity-refused
start stays ready for retry without pausing the receipt or requesting funding.
Continuations and landing recovery use the same capacity projection. Landing
recovery retains its request while waiting; its fixed deadline starts when
execution reopens and cannot be extended by retries. An unstarted request does
not block settlement after an external merge and verified rollout. A started
recovery still requires its recorded round and independent exact-head approval.

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

### Quota evidence and probe cleanup

KG admission needs at least one fresh permitting grant observation. Stale and exhausted
Codex grants cannot be selected beside fresh usable quota, including when token
fetch fails. After verifying that sidecar behavior on every brick, enable
`SWARM_CODEX_EXHAUSTED_GRANT_EXCLUSION=true` to stop exhausted accounts vetoing
a healthy account. It defaults off for safe independent chart promotion.
Before activating the sidecar's `prefer-first` policy, also set
`SWARM_CODEX_PREFERRED_GRANT=codex-b`: stale B and passed or unknown window
resets then defer admission, matching the stricter account selector.
Unknown or malformed evidence
and role floors still veto admission. Exhausted grants are reobserved without
inference to discover early manual resets. That alone still wedges an all-stale pool,
since the deferral stops the very traffic that would refresh an observation.
The tokenbroker's inference-free usage refresher is the primary source of
Codex grant freshness, including parked grants. The Codex probe is the
backstop: one unpinned Luna request when every observed grant is stale and
at least one is no longer exhausted. The ranker selects its grant, so a probe
cannot target a parked account. The gate and probe retain the sidecar's
15-minute freshness ceiling.

Probe cleanup advances past held rows in session-ID order before
limiting the result to five unheld guests. Bound or running Codex probes always
fence another probe, with a log naming the blocking reason. Only orphaned
pending turns and unknown outcomes older than 3,600 seconds stop fencing.
That gives recovery four times the 900-second freshness ceiling and thirty
120-second turn timeouts, without expiring a live guest's fence or clearing
its evidence.
(see: /projects/monolith/factory/quota_probe.py)
(see: /projects/monolith/factory/orchestration/model_pool.py)
(see: /projects/embervm/tokenbroker/cmd/tokenbroker/codex_usage.go)

### Autonomous intake and refine

The `human` issue label declares human ownership and takes precedence over
`agent-ready`, task class and allowlisting. It excludes both intake lanes and
queued admission. Observing it on admitted work durably fences further starts;
existing invocations drain through normal accounting before the receipt settles
`cancelled` with `human_handoff` evidence. Unknown invocation costs remain held.
Refine settlement checks the live label before readiness-label verification.
Landing disarms pending auto-merge and leaves the issue and PR to the human;
PR lifecycle does not draft or retire handed-off work. The handoff survives
label removal and requires explicit re-admission to restart the receipt.
`needs-human` remains the separate decision-request label.

`FACTORY_AGENT_BOARD_POLL_ENABLED` stages one board consumer at the
between-job boundary, before autonomous intake lists or claims another receipt.
It is false by default and independent of `AGENT_BOARD_ENABLED`. When enabled,
only an active, authorized `blocker:lane:<lane>` removes that lane from the
current intake and queued-receipt admission pass. The conductor polls once per
tick and passes the same eligible lanes to both boundaries. Deferred lanes are
audited at most once an hour. Expiry uses the server clock and becomes inactive
exactly at `expires_at`. A board read outage leaves the exclusive queue
available, and `claim:*` remains soft intent: it is ignored by this seam and
cannot replace receipt admission or `acquire_lock`.

The optional `intake` block is fully defaulted when an older policy does not
carry it:

```json
{
  "enabled": false,
  "labels": ["agent-ready"],
  "exclude_labels": ["needs-human", "wontfix"],
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
`exclude_labels` rejects an issue before selection. A `security-finding` issue
with `agent-ready` is an ordinary delivery candidate, subject to every other
eligibility fence. `human` ownership always excludes both lanes. Explicit
global exclusions and per-repository overrides remain authoritative. The staged
#7372 migration removes `security-finding` only from the two known legacy
global default lists; it leaves custom membership and repository overrides
unchanged. `max_per_day` counts
delivery admissions only and is between 1 and 10000, and `cooldown_hours` is
between 1 and 168. `refine_enabled` allows an
otherwise eligible issue with none of the delivery labels to enter the refine
path. `close_enabled` allows a refine verdict to close an issue and
`max_closes_per_day` bounds how many it may close in a rolling 24 hours,
default 3. All three feature flags default off or, for the cap, bound a
capability that is itself off.

An open `blocks` edge holds the blocked work item's receipt out of both the
autonomous candidate sweep and final admission. The sweep counts the exclusion
as `blocked`, and final admission records one `admission_blocked` audit for the
first queued receipt when it is blocked. Closing the blocking work item
releases the receipt on the next tick without operator action.

Block edges also derive from issue bodies. During each intake sweep, after
synchronizing GitHub issues as work items, the factory parses issue body text
for blocking phrases: "blocked by #N", "depends on #N", "waits on #N" all mean
that issue N blocks this one, and "blocks #N" means this issue blocks N.
All phrases are case-insensitive and accept both "#N" and "owner/repo#N"
formats, where "owner/repo#N" is only recognized if the repository matches the
current repo. Matches inside fenced code blocks are ignored, and a bare "#N"
without a phrase is never treated as a link. The factory reconciles these
derived edges on every sweep: four minutes after a completed idle sweep, immediately after a receipt
settles, and immediately after an admission. It adds edges for newly mentioned
issues and removes edges for issues no longer mentioned in the body. Manual and
decision-authored edges are never touched; only edges with source "github_body"
are updated. Block cycles in body edges produce a throttled
`work_item_edge_cycle` audit but do not prevent intake from proceeding.

Completed GitHub sweeps refresh after four minutes, including when all lanes
are full. The conductor runs a read-only observation pass in that case.
New or newly eligible work can therefore replenish an empty lane without an
operator command or a full hour of idle time. Failed, partial and unfinished
attempts retain the hourly retry bound. Only an evaluation recorded after the
latest attempt can shorten that bound; an older successful evaluation cannot
hide a newer failure. Shared GitHub quota cooldowns still gate every request.

Settlements and admissions reopen discovery on the next tick. Each sweep takes
at most one candidate per lane, and an admission buys one more sweep so lanes
can fill. Observation claims the aggregate sweep clock under the factory
control lock; replicas share that clock. A tick that
cannot read GitHub audits `intake_error`. Listing reads oldest-first, at most
20 pages of 25 issues and 10 pages of 25 pull requests per repository. The
500-issue and 250-PR coverage ceilings and the 1 MB response limit are unchanged.
The four-minute refresh therefore adds at most 450 listing requests per hour
per enabled repository, before admission-triggered sweeps and per-candidate
checks. Hitting a page cap records `truncated`; it never implies complete
repository coverage. Incomplete PR coverage excludes that repository's GitHub
candidates because an unseen PR may already own an issue. Local-authority
candidates remain independent. Delivery association likewise uses 25-row pages
and rejects incomplete scans at its existing 500-PR ceiling. Repeated idle
messages keep their independent hourly audit throttle.

The intake status exposes `last_swept` (the attempt clock), `last_evaluated`
(the completed candidate evaluation), and `last_error` (sanitized listing
failure evidence), each with its own timestamp. Every real sweep records its
listed counts, exclusions, candidate counts by lane, listing result and
truncation flag independently of the hourly idle audit. A later `not_due`
tick cannot erase this evidence. Candidate counts precede the daily cap and
the final locked admission checks; they are not admission counts. A failed
or partial GitHub read is explicit, and local candidates remain independent.
An old error is historical evidence, not proof that a newer sweep failed.

Each actual sweep stages one observation per open `agent-ready` issue it sees,
keyed by repository, issue number and the `intake_swept` audit ID. Local authority
wins over a duplicate GitHub representation. Selection exclusions retain their
specific reasons, including `blocked_by:#N` (the blocking issue number),
`excluded_label:<label>`, `cooldown_until:<UTC deadline>`, `refinement_failed`,
`capacity`, `daily_cap` and `not_selected`. Receipt refusal and failed discovery
have separate reasons. Malformed rows make coverage partial and cannot close
unseen work items. Failed work-item synchronization fences that repository's
GitHub candidates for the sweep.

`receive_issue` queues a receipt. The authoritative admission is the locked
`admit_next` transaction that creates the task and changes its receipt to
`admitted`. After its admission pass, the conductor finalizes staged evidence
into append-only `intake_candidate` rows and one private inventory snapshot.
Finalization is replay-safe under the same control lock and batches candidate
inserts. A standalone observation pass records queued receipts as `skipped`
with reason `queued`. Recovery finalizes abandoned staged evidence against
committed admission audits; an interrupted sweep before staging remains unfinished.
Caught evaluation failures downgrade complete discovery to partial coverage and
record `evaluation_failed` for candidates without a completed decision.
The legacy aggregate `intake_admitted` audit still marks receipt creation for
the existing sweep clock. It is not the admission oracle for candidate rows.

The private `/api/agents/factory` reader lists currently open agent-ready work
oldest first, using issue creation time, with latest decision, reason and
observation time. Never-considered issues have no decision. Partial, unavailable,
unfinished and stale evidence remains labelled. Freshness follows the existing
four-minute complete-sweep or hourly incomplete-sweep clock. A partial sweep
retains earlier observations with their original timestamps. Closed work leaves
the view; committed admissions after an observation are marked separately, even
if the receipt has already failed. A newer skip supersedes that admission. The
daily digest names up to three oldest currently skipped issues and their reasons,
excludes subsequently admitted and closed work, and names insufficient coverage.
The public snapshot publisher explicitly disables this private reader extension;
public routes and the public image gain no backlog fields or issue details.

Within an equal repository, lane and critical/bug priority group, autonomous
queued receipts prefer issue creation time and issue number for ties. Unknown issue
age sorts last. Groups retain their original queue position and operator receipts
retain FIFO. Receipt creation and later comments never reset autonomous issue age.

#### Staged acceptance for #7459

Repository delivery does not complete operational acceptance. Keep #7459 open:

- The authorized landing owner verifies the reviewed, Linux-CI-passing rollout
  and records its live revision before collecting acceptance evidence.
- Compare the private board with a fresh complete open agent-ready inventory,
  including age, latest reason, ordering, never-considered and coverage states.
  Confirm due full-capacity sweeps still record capacity skips.
- Observe two distinct completed sweeps: one candidate row per observed issue
  per sweep, actual blocker issue references, genuine admissions, cooldown
  deadlines, excluded labels, refinement failures, capacity and not-selected
  reasons, with no within-sweep duplicates.
- Report the first reason distribution, sweep times and inventory coverage on
  #7459 or its PR. Failed and truncated inventory remains incomplete.
- Verify the live digest's three oldest skips and the public serialization
  boundary against the deployed revision.
- A separately authorized operational owner checks and labels #6193, #6640,
  #6642 and #6649 appropriately, preserving ownership and admission fences.
- That owner checks previous comments and posts actually observed first-sweep
  reasons on #6621, #6622, #6625, #6630, #6631, #6632, #6634, #6635, #6636,
  #6662, #6659, #6482, #6306 and #6307. Report changed or closed targets.
  No script in this delivery performs these credential-backed writes.

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

A refine candidate carrying `needs-thought` is excluded as `deferred`, so an
autonomous `defer` verdict does not brief the same issue again after its
cooldown. A delivery candidate is still admitted when it also carries
`needs-thought`: an include label such as `agent-ready` determines that path
before the deferred exclusion is applied.

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

Operator-posted and allowlisted delivery receipts take a separate linked-PR
path. Before the receipt is written, the server performs the same bounded open
PR discovery used at first reconciliation. A same-repository `factory/` PR
that closes the issue is stored immediately as the receipt's delivery target,
so an operator repost or a later readmission continues that branch. A person's
branch or a branch held by another running task is never granted. Failed or
truncated discovery fails closed instead of creating an unbound receipt that
could open a competing PR. Ordinary autonomous discovery keeps the
`linked_pr` exclusion because it has no operator repost to authorize a new
receipt.

Delivery candidates rank before every refine candidate. Within either group,
`critical` ranks before `bug`, then the oldest issue ranks first. A refine
candidate carrying `critical` never outranks a delivery candidate without a
rank label.

Local-authority work items are a second intake source alongside GitHub. State
`ready` makes a local item a delivery candidate, while state `open` makes it a
refine candidate when `refine_enabled` is true. The same label, receipt,
cooldown, blocker, lane, and one-per-lane exclusions apply to both sources.
Local and GitHub candidates rank together by priority and age.

The `not_open`, `assigned`, `linked_pr`, and `pull_request` exclusions do not
apply to local items. The item is ours, and intake does not read back a human
closing or assigning its migrated issue on GitHub. A local item's labels are
ours too and never resync from GitHub.

Each intake receipt carries the task class that sets its verification mode,
implementer floor, and gate (ADR agents/038 decision 5):

| Class | Verification | Floor | Gate |
|---|---|---|---|
| `bug-fix`, `mechanical-refactor`, `docs` | machine-verified | the worker pool | independent review from the pinned reviewer pool plus required CI |
| `advisory-diagnosis`, `advisory-triage`, `refine` | advisory | the worker pool | none, because nothing merges |
| `judgment-analysis` | judgment | Opus or better | independent Opus review plus a human spot check |

Both intake paths classify. An issue an operator names in `issue_numbers` is
read for its labels exactly as a discovered one is, so the judgment floor
applies whether the lane found the work or was told to do it. Classes come from
the issue's labels in this order:
`security-finding` and `needs-thought` select `judgment-analysis`, `bug` selects
`bug-fix`, `documentation` selects `docs`, and `todo` selects
`mechanical-refactor`. An
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
DAG. It inspects the code, recorded decisions and current issue/PR state that
the verdict depends on. A focused knowledge query can supply relevant history;
confirm decisive claims against current evidence, even when marked verified.
Missing KG access does not block a repository-backed verdict. Web research
covers external facts needed for the decision.
The brief cites those sources under `### Evidence`, and after the verdict the
node reports its issue number, verdict and one-sentence reason back to the
knowledge graph on a best-effort basis. The node then posts exactly one
`## Agent brief` comment with `### Outcome`,
`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that order,
then reaches exactly one of five verdicts and acts on it:

| Verdict | What the node does | What settlement demands back from GitHub |
|---|---|---|
| `agent-ready` | applies `agent-ready` | the label, the brief, and the issue still open |
| `defer` | adds `### Why defer` naming the concrete condition that would make the issue actionable, applies `needs-thought`, and leaves the issue open | the label, the brief, the issue still open, and the condition in the artifact evidence |
| `needs-human` | applies `needs-human`, ends the brief with `### Decision needed` carrying `recommend: deliver \| close \| split` and the one quick-unblock question a person must answer | the label, the brief, the issue still open, and one warn notification naming the issue, the recommendation and the question |
| `reject` | adds `### Why not` citing a file, pull request or recorded decision, applies `wontfix`, closes with reason `not_planned` | the label, the brief, and the issue closed |
| `stale` | adds `### Why stale`, applies `stale`, creating the label if the repository has none, closes with reason `not_planned` | the label, the brief, and the issue closed |

`reject` and `stale` are for a premise a reader can check: a decision recorded
in an ARCHITECTURE.md **Why.** paragraph, work already merged, a file or flag
that is gone. Refinement chooses routine reversible designs within accepted
goals and scope and records the approach in Acceptance. `needs-human` is for
conflicting goals, missing authority, a constraint exception or an explicit
human-owned decision. `defer` requires a concrete prerequisite or external
event preventing progress. Missing design detail alone justifies neither.
Brief-only restrictions and typed gate effects still apply. A new
`leave_unmerged` hold requires an explicit unmerged-delivery scope; outstanding
live checks alone do not create one. Existing holds remain binding, and the
authorized landing owner still decides whether merge and rollout may proceed.

Closing is gated twice. It needs `close_enabled`, and it needs room under
`max_closes_per_day`, counted from the `intake_closed` audits of the last 24
hours. Both are read before the node runs, so the prompt offers three verdicts
rather than five when closing is unavailable, and again at settlement, so a cap
spent while the node was running still holds. A close verdict the lane may not
act on is downgraded to `needs-human`: the server then demands the
`needs-human` label and an open issue, exactly as it would for an escalation,
and records `refine_close_downgraded`. An issue carrying `critical` or
`security-finding`, or assigned to any milestone, is never closed and takes the
same downgrade. Every close that settles audits `intake_closed` with the
evidence cited.

A `reject` that finds the work belongs in another issue can list up to ten
other open issues in `supersedes`, but doing so also requires the surviving
issue in `in_favour_of`. The server refuses the relationship unless that
favoured issue is open, is not a pull request, and is neither in `supersedes`
nor the issue the node briefed. The node closes only the issue it briefed.
After verifying that primary close, the server re-reads each sibling, comments
with an idempotency marker, applies `wontfix`, closes it as `not_planned`, and
writes its own `intake_closed` audit. It skips and audits a sibling that is a
pull request, already closed, protected by a label or milestone, assigned,
represented by an active or escalated receipt, or beyond the daily close cap.
The marker proves only that the comment landed. On a retry the server re-reads
the sibling: if it is closed, it fences in the missing audit; if it is still
open, it repeats every other guard, then retries the label and close without a
second comment. One sibling failure is also audited and does not fail
settlement of the verified brief. If an operator later chooses `supersede` and
the receipt's own issue is the survivor, the server re-queues that receipt for
a fresh brief that includes the folded-in scope.

The server settles from a re-read of the issue, never from the node artifact
alone. A verified `needs-human` result succeeds because the briefing and the
escalation both completed, and a verified close succeeds because the issue is
demonstrably closed with its reason on record. A mismatch fails the task and
the issue takes the cooldown. Two failed attempts settle the task failed and
apply nothing. So does any other limit that leaves the node unable to run:
advisory work has one node and no planner to fall back on, so a tick that finds
no attempt in flight, no success, and the node absent from the conductor's
ready set settles `refine_failed` there and then, naming the limit it hit.
Idling instead held the advisory slot and its reservation for ever (#6045).

### GitHub read pacing

The factory GitHub reader shares a monotonic cooldown across its process's
callers and repositories after an observed rate limit, or a successful response
that consumes the last request. It respects the response's reset and retry
headers, and backs off when those headers are absent or stale. Calls during the
cooldown fail before network I/O and do not supply cached ownership evidence.
The same reader emits bounded minute-window counters for completed network
attempts, response status families and reads deferred without network I/O.
Endpoint families are fixed labels, never repository names, object IDs, query
strings or credentials. Observed quota consumption includes every caller
sharing that provider quota and is reported separately from local requests.
Concurrent response order and quota resets cannot manufacture consumption.
These process-local diagnostics add no requests and grant no authority.
Rate-limit diagnostic logs contain validated rate fields and the next eligible
read time, without tokens, error bodies or query strings.

Rollout ancestry observations between two full commit SHAs use a separate
4096-entry cache containing only the comparison status. File responses retain
their existing 256-entry bound. Unknown statuses and read failures
are not cached, while ownership, CI, publication listings and live deployment
observations retain their existing freshness checks. Endpoint counters separate
`compare`, `contents` and `git` reads so runtime improvement remains measurable.

A later natural tick reads GitHub again after the deadline. This is process
pacing, not a distributed quota ledger: after a leader replacement, its first
response establishes the new process's wait. Requests already in flight are not
cancelled, and other clients consuming the same provider quota are unchanged.

(see: /projects/monolith/factory/orchestration/github_read_cooldown.py)
(see: /projects/monolith/factory/orchestration/factory_rollout.py)

### Queue-driven backlog refill

The optional top-level `backlog_refill` block is omitted from shipped policy.
Omission and `enabled: false` start nothing. Repository defaults are:

```json
{
  "backlog_refill": {
    "enabled": false,
    "min_eligible": 25,
    "cooldown_minutes": 15,
    "min_new_issues": 3,
    "breaker_consecutive_low_yield": 3,
    "max_per_24_hours": 24,
    "model": "astra",
    "effort": "high"
  }
}
```

The recorded decision on #7376 sets the daily default and upper bound to 24.
The illustrative 48 in the original issue is superseded. The floor accepts
1..1000, cooldown 1..1440 minutes, yield 0..100, low-yield breaker 1..10 and
daily starts 1..24. An enabled or explicitly selected model must belong to
`allowed_models`. Unknown keys and invalid types are rejected.

The intake sweep supplies its own filtered agent-ready issue count before
lane capacity filtering. Advisory and PR candidates are excluded. Incomplete,
failed or stale discovery cannot establish a low backlog. A selected refinement
batch suspends refill starts and admission. Below the floor,
one durable intent and advisory receipt reserve the run before dispatch.
Normal admission, task budgets, quota and transport controls still apply.
The existing sweep pacing is unchanged; there is no calendar schedule.
Settlement, fresh intake evidence and the cooldown permit another run.

New starts pin two single-attempt read-only roles. The configured generator
runs one maintenance routine, recorded as `routine` on the start and rotated in
order: `dead-code`, `finished-flags`, `test-gaps`, `useless-tests` and
`abstraction-police`. Each routine in `backlog_refill.ROUTINES` is one sentence
naming the outcome; the collector chooses its own static and dynamic analysis.
A start without `routine` uses the first. Selecting `opus` through the bounded
refill control uses the deployed Opus mapping. An independent xhigh review
session challenges every finding, rejects duplicates and unready scope, and
supplies executable plans and acceptance checks. The review runs on Astra; a
start pins Opus instead while cached quota walls Astra and policy allows Opus,
as other pools spill. Each dispatch requires fresh quota for its pinned model.
New collections contain at most five findings, a 12,000-character summary and
3,000 characters per finding body. The schema excludes non-whitespace control
characters and lone Unicode surrogates. Lossless Unicode JSON rendering keeps
the full collection and review instructions within 60,000 characters, including
worst-case escaping. The independent summary is limited to 12,000 characters.
Legacy dispatch retains the original schema and limits.

The producer verifies both graph runs, start reservations, immutable pins and
distinct server-owned sessions. The review must bind the collection digest and
decide every finding before any issue write. Only approved findings become
agent-ready issues. A routine run's summary stays in its settled audit; a start
recorded before routines still publishes one needs-human inventory summary.
Legacy starts without the review contract retain their original single-role behavior and are
explicitly recorded as unreviewed. Existing ledgers are never upgraded in place.
The labels are fixed. Every output has a run marker and a separate
stable output marker. Finding identity derives from the normalized title, so
renaming the model's key cannot count the same title as fresh yield. This is
conservative title matching. The independent reviewer also checks semantic
duplicates against current open work.

A pre-write intent commits before POST. Immediately before that commit, the
producer rechecks the owning receipt, pause/cancellation state, generation,
policy and deadline under the control lock, after discovery I/O. An uncertain
write receives six read-only reconciliation attempts. If it remains unknown,
only that output is quarantined: it can never be posted again, while the run
settles inconclusive and later inventories can proceed. Maintenance reads one
due original quarantined output per tick, with a 24-hour interval per output;
positive marker discovery resolves it without another POST. Reset, cancellation
and disable retain the intent and allow read-only reconciliation.

Reaching `breaker_consecutive_low_yield` consecutive conclusive runs (default
3) below `min_new_issues`, or two consecutive failed runs, opens the breaker.
Failed runs count toward low yield too. A yield at or above the minimum resets
the low-yield streak; any successful run resets the failure streak. Interrupted
runs and unresolved publication are inconclusive and do not advance or reset
either streak. Paused tasks can settle at their deadline once execution is
quiescent; unresolved dispatches retain their run fence. Every breaker opening
owns one needs-human issue titled `factory: backlog refill breaker open`, with
its triggering summaries. Re-posting policy resets the breaker and streaks,
including an unchanged post, while retaining output intents and rolling start
accounting. `factory_configure_refill` keeps the breaker open unless the
operator passes `reset_breaker: true`, which records the same reset without a
full policy post or a generation change. Its optional `min_new_issues` sets
the low-yield floor the same way.

The daily-start limit pauses starts until the rolling 24-hour window has room.
It resumes without a policy re-post. The board shows off, armed, in flight,
rate limited or breaker open, plus the rolling start count.

This is a repository-only, default-off implementation. Operational acceptance
remains on #7376: verify the omitted-policy rollout and ordinary intake; after
the #7359 lane drains, observe below/at-floor scheduling in advisory capacity;
verify inventory outputs, markers and yield; exercise a staged restart and
uncertain-outcome drill; then exercise each breaker, rolling-limit recovery,
reset and disable path. Live enablement remains outside this default-off
landing.

### Exact-event problem issues

The optional top-level `problem_issues` policy block turns three demonstrated
factory failure signals into ordinary GitHub issues: `node_stalled`,
`workflow_stranded`, and terminal `landing_recovery_exhausted`. It does not
create a receipt, post an intake event, or admit a task. Generated issues can
enter the factory only when the existing GitHub discovery and refine/intake
policy later selects them.

The block and every source switch default false. Its repository defaults are:

```json
{
  "problem_issues": {
    "enabled": false,
    "sources": {
      "node_stalled": false,
      "workflow_stranded": false,
      "landing_recovery_exhausted": false
    },
    "source_audit_limit": 50,
    "issue_pages": 2,
    "issues_per_page": 100,
    "max_per_tick": 1,
    "max_per_24_hours": 3,
    "labels": ["bug"],
    "retry_minutes": [2, 4, 8, 16, 32, 60]
  }
}
```

The first tick after the top-level switch or an individual source is enabled
records `problem_issue_policy_observed` with a watermark at the newest matching
source audit. History from before enablement is never replayed. Each later tick
scans at most 50 exact source audits and creates at most one issue. The rolling
24-hour cap counts durable `problem_issue_write_started` intents, including an
ambiguous write, so uncertainty cannot buy extra external writes.

Every body links the delivery issue, links the pull request when the signal is
about landing, names the factory task and source audit, and contains an exact
`factory-problem` fingerprint marker. The receipt's repository owns discovery,
links and creation even if later global policy names another repository.
Discovery inspects at most two pages of 100 newest open or closed issues for
that marker. If both pages are full, the producer audits the bounded scan and
continues from its durable pre-write ledger; otherwise a repository with more
than 200 issues could never produce a new issue. Pull requests returned by the
GitHub issues API never satisfy an issue marker. A repeated or replayed source
event reconciles the existing issue. Fingerprint lookups use the audit trail's
action index and database predicates rather than loading producer history. A
missing source receipt, malformed event, discovery failure, scan cap, daily
cap, or rejected GitHub write is audited and visible on the factory board.

The producer records `problem_issue_write_started` before the GitHub request.
A pending intent retains its original repository even if later policy points at
a different repository. A definite client refusal is terminal. A timeout, rate
limit (including a rate-limit `403`), transport loss, server error, oversized
response, or response that does not confirm the marker is ambiguous. Response
bytes are streamed into the documented bounded buffer. Disabling the producer
stops new writes but lets these read-only reconciliations finish. The lane
does not repeat that create request. It performs six marker reconciliation
reads after 2, 4, 8, 16, 32, and 60 minutes, recording
`problem_issue_write_uncertain`, `problem_issue_reconcile_retry`, and finally
either `problem_issue_reconciled` or `problem_issue_unresolved`. This makes a
crash immediately before the request conservative too: it can omit an issue,
but cannot create the same one twice by guessing whether the write happened.

Repository delivery is staged and does not complete #6002 operationally. The
rollout checklist remains:

- Deploy with `problem_issues` omitted or disabled. Confirm there are no issue
  writes and the board reports the producer off without affecting factory
  reconciliation.
- Enable one source in a staged window and replay one safe known audit. Confirm
  exactly one issue has only the `bug` label, the exact marker and source links,
  with no receipt or task directly admitted.
- Repeat the event across a reconciler restart and an uncertain-write drill.
  Confirm marker reconciliation, visible retry/backoff audits, no duplicate,
  the one-per-tick cap and the three-per-day cap.
- Leave intake/refine policy unchanged, observe the generated issue using only
  normal GitHub discovery, then disable the source and confirm writes stop.
- Validate phase 4 chart write-back and live rollout separately before any
  future change claims the parent programme complete.

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

`effect` is one of six. `agent-ready` applies the delivery label and takes
`needs-human` off, with an optional scope note posted as a comment. `close`
comments the reason and closes with `not_planned` or `completed`. `split`
opens one to five child issues, then closes the parent against them. `defer`
applies `needs-thought`, takes `needs-human` off, and comments the condition
that would make the work worth doing. `hold` writes nothing and records that
someone looked and chose to leave it. `supersede` closes one to ten named
issues as `wontfix` in favour of one surviving issue. The receipt's own issue
must be among the closes or be the survivor.

Each child created by `split` carries a `parent` edge from the split issue's
work item.

The escalation context document presents five sections: the ask, what stopped,
what happened, cost, and lineage. Each section's `line` is a claim, and the
fields beneath it are the evidence for that claim. The private read endpoint is
`GET /api/agents/factory/escalations/{receipt_id}/context`. It reads only the
factory database and performs no GitHub reads.

The private card renders the five context lines above its options, and pressing
`e` expands their evidence.

The first option is the recommendation, and its effect has to be the one the
`recommend:` line names: deliver is `agent-ready`, close is `close` or
`supersede`, and split is `split`. `defer` remains available as an alternative
but cannot be recommended, because a node that reaches that conclusion takes
the autonomous verdict. The prompt asks for labels that name the concrete
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
it as the comment.

The dismiss is the one resolution that is not final, and it has to be, because
it is one keypress with no confirmation. Because it wrote nothing, the issue
is still exactly what it was, so a later decision, a chat request, and a fresh
brief are all still accepted on a dismissed escalation: the claim it holds is
superseded the way a failed one is, `_resolve` overwrites it rather than
returning it, and settlement replaces the whole document so the card comes
back carrying the answer. Every other resolution stays final. This is a
property of the record, not of the page, and the page has no control that
un-dismisses a card: the way back is a chat request or another decision
through `POST /api/agents/factory/decisions/{receipt_id}`. The keys carry a colon, which the option key pattern
forbids, so a brief can never author one that collides; the brief's own
options keep the numbers 1 to 4 and the escapes answer to `x`, `d` and `Esc`.

`escape:close` takes its close reason from the option's `detail`, the same way
the brief's own `close` does, so the two read one field rather than one of
them hardcoding `not_planned` beside a `detail` nothing consulted. It is never
weighed against the protected-label rule that downgrades a node's own close on
a `critical` or `security-finding` issue.
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
operator gate as `/control`, with an exact `decision_id`, a stable
`request_key`, and either `{"option_key": "...", "note": "..."}` or
`{"action": "chat", "note": "..."}`. The private factory page at
`/factory/escalations` reaches the same code through
`POST /api/agents/factory/decisions/{receipt_id}`, which the browser can use
because it is gated on `X-Auth-Email`, the address Envoy projected from the
verified Access JWT and the gateway strips on ingress so it cannot be
smuggled. Not `Cf-Access-Authenticated-User-Email`: nothing in the cluster
validates or strips that one, so a caller reaching the backend can set it to
any address. A request carrying no projected address, two of them, or only the
Cloudflare header is refused.

Both HTTP routes and MCP call `factory_decisions.request_decision`. The owner
persists the accepted or refused outcome under the verified actor before any
external effect. The browser hashes the exact receipt, decision, option or chat
action, and note into a stable request key, so retry after response loss returns
the original acknowledgement. No HTTP route may bypass this contract through
the legacy direct apply or chat helpers.

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
issue that is neither in the policy allowlist nor discoverable with intake on.
A generation-stale chat is refused before its issue comment is posted, because
the retirement path owns that card and the old receipt can never be selected by
the current policy.

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
against `max_review_rounds`. Each correction and re-review has one attempt;
failed corrections consume a round instead of retrying within it. Exhausted
correction rounds return to the planner. A failed re-review consumes its round
and returns missing evidence directly to the planner under
[execution and evidence](#execution-and-evidence); it does not open another
correction against the earlier findings.

An operator can set `max_review_recovery_rounds` to 1 or 2 to continue a
review that still requests changes after the ordinary round bound. The default
is 0. Recovery requires an open PR on this task's branch in this repository,
the configured base branch, the exact head recorded by the latest review,
and both successful `pr-checks` and successful aggregate commit status. Draft
PRs can be corrected. The PR identity is read again after CI to detect a head
change during the observation. Missing/pending CI and transient GitHub reads
wait on the same task without a planner turn; red checks or changed PR identity
return to planning. Waiting remains subject to the original task deadline.

Recovery is another engine-owned correction and independent re-review pair,
not a new admission. Its starts and dollars count against the original task
envelope, and its deadline, receipt, branch and history remain unchanged. The
round count comes from the durable graph, so restarting the reconciler does
not replenish it. The engine checks each pair against the remaining envelope
when inserting it, without reserving conditional recovery before it qualifies.
`review_recovery_observed` audits the evidence, and the inserted graph records
the CI head in the correction's reason. Failed corrections do not qualify for
these extra rounds, and `max_review_rounds: 0` still disables all corrections.
Neither agents nor planner decisions can change either server-owned bound.
After recovery is spent, the existing planner/escalation path applies. This
field is pinned at admission along with the policy; enabling it affects future
admissions and does not alter in-flight pins or resolve existing escalations.

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
edit whose derived turn allowance would exceed the envelope is refused whole
with `envelope_exceeded` and a detail naming needed against allowed turns,
beside `spare_turns`, what the envelope would still fund once the reserve that
edit brings with it is counted. Dollars are reported in the preview but never
refuse an edit (#7161). The planner reads that in `decision_feedback` and
answers by shrinking the edit, splitting the work, or pausing; it must not
re-propose a refused edit unchanged. Discarding a node drops its unspent slots and never refunds a
consumed turn.

The example is documentation, not live authorization. Use the selected issue,
current capacity and an explicitly accepted policy for an operating trial.
Admitted tasks retain their policy even if later configuration changes.
The initial worker model uses the proven Luna route. Muse can be selected after
its actual dispatcher and guest route are verified on the deployed stack.

### Completion accounting

A factory start reserves a ceiling and settles at the cost its node result
reports. Codex-backed models report no provider cost, so a result falls back to
the list price the turn store computed from token usage, and every result
carries the basis it settled on: provider, list, or unknown. The graph surfaces
that as an accounting basis of reported or list_priced beside the reserved and
accounted figures. Only known completions settle at a priced amount. Unknown
execution retains its whole reservation, and a completion with no price at all
still consumes its ceiling. The receipt counts work starts as `turns_used` and
conductor rounds separately as `planner_turns_used`. Work starts meet
`max_turns_per_task` and planning rounds meet `max_planner_turns`, an optional
policy field that inherits the work cap when a policy predates it.
(see: /projects/monolith/factory/orchestration/factory_controls.py)
(see: /projects/monolith/factory/orchestration/node_workflows.py)
(see: /projects/monolith/shared/pricing.py)

The turn store's token-derived list price distinguishes a priceable Codex
completion from an unknown cost. An unpriced completion consumes its ceiling;
unknown execution retains the reservation. Priced completions settle their
ledger without discarding delivered work (#7161).

## Execution and evidence

The reconciler reads a fresh control row and expands only active receipts using
the same active-task snapshot as operator status. It retains old generations,
paused work, cancellation, unresolved starts, accounting and direction. Landing
keeps its separate reader and status retains history; execution authority is
never cached to avoid reading completed receipts.

Source nodes (`implement_*`, `correct_<round>` and `integrate_*`) receive a
server-authored push-format contract through the existing session transport.
The contract matches the exact task, node, attempt and dispatch key. It accepts
an unbound run because the first pending turn can dispatch before graph binding;
a run bound to another session is rejected.
Browser-capture, planner and review nodes receive none. The shared runtime shim
installs a chained pre-push hook before the model runs. It validates the exact
outgoing commit in a detached linked worktree with changed-file `ci lint`,
`ci regen`, then `ci lint` for generated outputs. Missing tools, a missing
baseline, command failures and changed outputs refuse the push. The hook prints
the exact drift paths and retains the validation worktree so the worker can
incorporate its outputs and commit before retrying. It never formats the
worker's dirty checkout or transient capture files. Existing hooks still run
with their original arguments and stdin; ordinary human branches keep their
existing behavior. Hooks protect against accidental drift; CI remains the
backstop against deliberately disabled hooks.

The guest retains a per-node union of drifted paths under the shared Git
directory. The shim includes it in native result usage even after a successful
correction. The existing result transaction writes `format_drift_prevented`
with task/node identity and the distinct newly observed file count. The task row
lock and existing audit trail fence replay and duplicate result processing.
Clean checks and missing-tool failures contribute no saving. No durable workflow
checkpoint is added or reordered.
Invalid guest evidence records a bounded `format_drift_evidence_rejected` audit
under the admitted server identity and does not prevent saving the completed turn.

Experiment #7458 is staged repository delivery. The hypothesis is that validating
outgoing commits in guests reduces factory PR format writebacks from the issue's
baseline of about 35 per week to fewer than 5 per week. The operations owner
starts a 14-day window only after recording the deployed monolith and runtime
revisions and verifying fresh implement, correct and integrate guests. Each
seven-day interval must have fewer than 5 `style: auto-format` commits by
`ci-format-bot`, attributed to originating `factory/` PRs with raw revision and
time-range evidence. Verify deployed audit identity, distinct file counts and
replay behavior separately. Leave the issue open until these checks are accepted.
Stop and revert this change through reviewed GitOps delivery if validation blocks
correct trees, alters unrelated files or cannot run its packaged tools; revise
or revert after 14 days if the threshold is missed. No live improvement is claimed.

Rollout checklist: changes to `rules_go`, `rules_python`, `gazelle`, `skylib` or
`bazel/helm/gazelle` sources invalidate the packaged generator fingerprint. Until
rebuilt runtime images roll out, `gazelle_inputs verify` refuses guest pushes
touching Python, Go or BUILD files, including the PR changing those inputs.
Keep validation fail-closed. The authorized landing and operations owner must
coordinate generator-input delivery and rebuilt images before fresh source
guests can pass validation; record the affected runtime revisions in acceptance.

Each graph admission and factory turn reservation commit atomically. The
node's deterministic session identity is reused after an interrupted submit.
A workflow replay cannot read a different graph or silently replenish its
attempt, task-turn, deadline or budget bounds. One failure does not count
against `max_attempts`: a session create the control plane refused with a 429
capacity denial never reached a model, so the run records `capacity_denied`,
audits it against the task, and is excluded from the attempt count by both
`graph.admit_dispatch` and the conductor's readiness. At most three such
denials per node are excluded, after which they count like any other failure,
so a saturated control plane still retires the node. The start ledger records
the same verdict in `factory_start.accounting_basis` and excuses the turn and
the budget an excused denial would otherwise spend. The two ledgers have to
agree: a node the graph keeps ready whose every start the turn gate refuses
pauses the task instead of retrying it. Confirmed failed artifacts feed
bounded retry context back to the next attempt. Unknown execution retains its
reservation and requires reconciliation before another attempt starts.

A failed re-review keeps the delivered correction and returns the missing review
evidence to the planner as a named deviation. A lost or invalid verdict supplies
no new findings against that head. The planner resolves the missing review
evidence within the task's normal limits; the engine never re-implements the
correction against superseded findings. Result adoption and cessation fences
still own unknown invocation outcomes.

The lost-before-session sweep runs before ordinary task reconciliation and the
deadline backstop, after the node's pinned `turn_timeout_seconds` has elapsed
(#6285). `swarm.factoryLostBeforeSessionSweepEnabled` maps to
`FACTORY_LOST_BEFORE_SESSION_SWEEP_ENABLED`. The chart default is false;
`projects/monolith/deploy/values-gke.yaml` sets it true. The sweep does not widen
manual repair, session-based reconciliation or uncertain-execution release.

The exact owning DBOS workflow must report `ERROR` or `CANCELLED`, or a successful
lookup must explicitly prove it absent. Under the control lock shared with
session creation, the proof requires immutable run and start ownership, no bound
or deterministic session, no cost or outcome, and no permit or result receipt.
The terminal graph transition fences a delayed submitter, including the
absent-workflow case. A nonterminal workflow, failed ownership or lookup error
leaves the attempt untouched. Both ledgers settle atomically as `failed` at zero
cost with `no_model_post` and `never_dispatched`. Ordinary bounded reconciliation
then decides whether the node may retry. Elapsed time or a missing binding alone
cannot refund an attempt with contradictory execution evidence.

The DBOS application version is pinned to the node workflow's own source:
`execute_node`, the helpers that decide which steps run, and every step they
call. DBOS otherwise derives it from every registered workflow in the process
and neither recovers nor dequeues anything an older version started, so any
unrelated workflow edit stranded in-flight nodes. Pinned, a deploy that leaves
those functions alone recovers its in-flight nodes natively. Other workflows
keep the same version when edited and replay against their recorded steps.
DBOS patching is enabled alongside the explicit node version; it does not
replace that version.

`drain_cycle`, `implement_then_review` and `start_session_workflow` must preserve
their checkpoint sequence across deploys. The current sequence is the baseline,
with no patch markers. Do not wrap an existing unchanged call in a patch.
Gate every future addition, removal, reorder or rename of an operation that
consumes a workflow function id. In DBOS 2.29.0 this includes `@DBOS.step` calls,
including calls from helpers such as `_claim_with_idle_wait`, `_await_turn` and
`chain_next_cycle`; durable operations such as `DBOS.sleep`, send/recv and
set_event/get_event; child workflow calls, starts and enqueues; and patch markers
themselves. The rule also applies to other checkpointing DBOS APIs and their
async variants.

To add a step, use `if DBOS.patch("<unique-name>"): new_step()`. To remove one,
use `if not DBOS.patch("<unique-name>"): old_step()`. Reorder or rename with a
removal and an addition, each with its own unique patch name. Preserve the old
branch for pre-patch history. Put behaviour that needs no checkpoint of its own
inside an existing step or in non-step code. Retire a patch with
`DBOS.deprecate_patch` only once no workflow started before the patch can still
be PENDING; drop it in a later deploy after histories containing that marker
can no longer replay.

**Why.** #6659 added `_quota_span_attributes` ahead of recorded drainer steps.
Replay raised `DBOSUnexpectedStepError` after reservation and before session
creation, orphaning permit 10732. A patch returns false on pre-patch history
without consuming a function id, and records a marker for new executions.

Editing the node workflow or one of its steps is therefore the one deploy that
strands in-flight nodes: DBOS does not recover the older version, so patching
does not help there. The reconciler cancels
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

Stop supervision also checks the Kubernetes node inventory. When an old parked
or banked guest names a node that is no longer in the cluster, supervision asks
Ember to destroy it and waits for the next observation to prove `destroyed`
before settling the attempt. The destroy carries the observed generation and
consumes one of two durable request slots before the network call. Exhaustion
requires operator intervention. An unavailable inventory never counts as an
empty cluster. If a factory session is still `recovering` after its DBOS
workflow is terminal and its executor claim is older than fifteen minutes, the
conductor records the turn as an unknown invocation first so the same stop
supervision path can own the guest. Issue #6091 tracks the control-plane root
cause.

Receipt adoption is an optional observation of the original model request.
If the receipt poll rejects ownership or its captured result cannot be parsed,
adoption stops while the original request remains bounded by its existing
transport deadline. The normal result writer still checks exact ownership before
saving that response, so an expired heartbeat cannot cancel healthy work and a
replaced owner cannot overwrite the current attempt. If the original request
fails, an unavailable or rejected final receipt read preserves that original
failure for transport recovery and accounting. Cancellation still releases the
original observer; no fallback starts another model request.

A terminal factory workflow may also recover a completed native result whose
pending claim was already deleted by unknown-outcome settlement. Before remote
stop supervision, the conductor validates the exact run, start, session, permit
and complete physical dispatch receipt chain under the existing ownership locks.
Every earlier response must be an authenticated drain, ordered before the next
dispatch. The final response must be unsuperseded, retained, hash-valid and
completed, with matching guest, CLI, logical turn and physical dispatch identity.
Missing receipts, aliases, newer work, cleanup claims and changed bindings retain
the hold. This path performs no guest invocation, stop or restart.

Recovery replaces the failed native turn atomically with the completed result,
settles its capacity permit, and writes `completed_receipt_recovered`. The full
original failed turn remains in `usage.factory_receipt_recovery.previous_turn`,
alongside the exact identity and receipt provenance. An unobserved response keeps
a guest-reuse fence until its observer releases it. A single dispatch retains its
native cost; a drain prefix retains unknown aggregate cost rather than pricing
only the final response. Normal conductor artifact validation and graph/start
accounting still decide the outcome. A missing artifact fails the node and unknown
cost consumes the full reservation. A crash after native adoption can replay this
normal settlement without another model call.

A drained factory turn has a separate settlement path. The normal pending-row
rejection remains unchanged because the row is normally the valid continuation
grant. With `FACTORY_DRAINED_LOSS_SETTLEMENT_ENABLED=true`, the factory consumer
may remove that row only when every local identity still matches under the
factory control, capacity-pool, session, turn, pending-message, permit, run and
start locks. The session must still be `recovering`; the pending message must be
unclaimed; and its turn, dispatch count, permit owner, guest, lineage and CLI
transcript must still describe the same first factory attempt. A newer turn,
dispatch, guest, claim, receipt fence, cleanup claim, prior binding or relight
refuses settlement.

The remote proof is equally narrow. Ember must return the exact guest as
durably `evicted` with terminal reason `node_gone`, a transition its dormant
departure reconciler writes only after the owning brick is authoritatively gone
and neither a surviving local artifact nor an exported bundle has a valid
relight target. Its persisted `interrupted_turn` must match the opaque dispatch
ID, turn sequence, CLI session and transcript path computed from the local
rows. This is the paired evidence that the drained dispatch finished its flush
and then permanently lost its only restoration paths. `failed/brick_gone`
proves cessation but not permanent loss and is insufficient here. A missing
guest, generic `destroyed`, timeout, API failure, malformed or stale marker, or
a guest that is running, banking, banked, parked or relighting leaves the
continuation untouched. Settlement keeps the interrupted turn and its result,
transcript and cost history, marks the attempt failed without calling it
`not_invoked` or `UNKNOWN_INVOCATION`, releases its reservation, and feeds the
ordinary bounded replanning path.

The feature is staged off in chart defaults. Enabling it and any production
row remediation are separate operator actions. Before enabling it, exercise a
real disposable factory drain through the consumer and verify both the exact
`evicted/node_gone` settlement and refusal of `failed/brick_gone`, live,
restorable, stale-dispatch and unavailable-control-plane observations. Sessions
8410 and 8416 are not changed by this repository delivery; their remediation
and the final issue acceptance remain separately authorized live checks.

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

Receipt reads use the durable, dispatch-exact hold in place of the live
30-second claim stamp the lost executor can no longer refresh. An executor-written
hold names one receipt and carries the request digest, generation and invoke
start; other ownership checks remain unchanged. Held adoption sets no guest reuse
fence: the original POST observer is gone and cannot clear one. The hold itself
fences reuse until the ordinary turn writer consumes it.

The 2026-10-01 evidence in #6670 identified eleven cessations during monolith
promotions. `agents.sessions.rolloutHandoffEnabled` now fences claims at SIGTERM
and hands off the exact in-flight POST before receipt-observer cancellation
cleanup or DBOS destruction. The existing cancellation hold remains the
flag-off path. A turn without an adoptable receipt gets a five-second grace;
hold writes and cancellation each have a five-second drain budget inside the
chart's thirty-second termination grace. Shutdown hold writes use a two-second
row-lock timeout. The enabled backend bounds HTTP drain at five seconds before
lifespan handoff. The flag defaults false and the GKE overlay enables it with a
dated comment. The file-backed two-replica test rolls
the observer mid-invoke, adopts its receipt through the ordinary writer, and
checks the node workflow succeeds. It does not cover a control-plane restart
(#6664) or cross-brick rejoin (#6663).

A queued executor that receives a result but fails to persist it also writes
this bounded hold before releasing its claim. Receipt adoption can retry the
database write without a second model invocation or a live-guest probe. The
hold writer refuses changed ownership and already-committed terminal results;
if recovery is disabled or the hold cannot be written, the existing conservative
unknown-outcome path remains. Historical attempts whose claims were already
removed still require separate positive reconciliation evidence.

A replica killed outright writes no marker. The claim lease backstop can hold a
stale claim with an exact, unsuperseded, unconsumed receipt, including one whose
result has not yet been published. An unpublished receipt's remaining capture
window bounds that hold. Registration alone proves no delivery or completion;
only the ordinary validated result writer can finish it. A reconstructed hold
has no request digest and selects its receipt by full dispatch identity. Claims
without an exact receipt retain ordinary unknown settlement.

A factory guest cold-parked without a recorded drain continuation is a candidate
for conditional retirement, not cessation proof by itself. Recovery asks EmberVM
to retire the exact session ID, generation, invoke-start and update timestamps.
The manager refuses a changed snapshot, live residency, registered process,
queued wake or pressure retry; accepted retirement is durable before replying.
The factory retains its hold until a later GET confirms the terminal session.
A concurrent native result wins under the receipt lock. Existing supervision
then matches the terminal invocation to the failed dispatch and settles its
permit, node, and start together, preserving unknown or recorded cost. Old
servers, malformed observations and transient errors leave the hold intact.

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

A separate control-plane result cache is not planned. The #4322 decision is
reconsidered only if #5938's bounded native canary demonstrates a completed-result
recovery gap that the receipt path cannot cover and a bounded receipt fix cannot
address.

The pre-guest response-loss window is intentionally separate. A response-loss
hold requires the exact persisted guest binding, result receipt, executor
claim, dispatch count, and permit. If the executor is lost before a guest is
bound, there is no receipt to recover and no remote invocation to resume. The
session instead records `invocation_outcome_unknown`; the
`lost_before_guest` proof requires a terminal failed session, no current or
prior binding, no pending executor, no receipt or work product, and one exact
uncertain permit. The factory may then fail the attempt at zero cost and
release that reservation exactly once. Changing
`agents.sessions.responseLostRecoveryEnabled` does not widen or disable this
proof. A bound guest, including one whose generation or invoke stamp changed,
remains on the response-loss and stop-supervision paths and is never refunded
by `lost_before_guest`. A message with dispatch count zero remains the separate
`never_dispatched` proof.

A guest that answers its turn with a provider error before doing any work is a
proven failure, not an unknown one (#6468). The Claude CLI reports a refused
request, such as a 400 for a model its version does not support, as a native
result with `terminal_reason` `api_error`. `execute_node` still records that
turn as uncertain, because its terminal reason is not clean, and the native
writer leaves its permit uncertain. The conductor then reads the
`provider_error_before_work` proof: the attempt's one turn carries a
`PROVIDER_ERROR_TERMINAL_REASONS` value that only a guest writes, no
`UNKNOWN_INVOCATION` or recovery marker, no tool activity, no permission
request, no diff and no artifact, with no pending message and a terminal
workflow. The returned result is itself the cessation evidence, so the proof
settles the permit, fails the graph attempt with reason
`provider_error_before_work: <error text>` at the reported spend (not the
reservation), and settles the start, all in one transaction. The node's
ordinary `max_attempts` then applies to the retry. Any tool activity before the
error, a lost response, a timeout or an unknown invocation keeps the attempt on
the uncertain path exactly as before. The rule is not in `execute_node`, so the
pinned node workflow version is unchanged and an attempt already recorded
uncertain in this shape settles on its task's next reconciler tick.

#### Bound zero-turn settlement runbook

`FACTORY_BOUND_ZERO_TURN_SETTLEMENT_ENABLED` stages the post-guest proof from
#6288 and defaults to false in both chart defaults and the GKE overlay. Its
live validation is tracked on #6559. It is not a general uncertain-attempt
sweeper. The owning DBOS workflow must already
be terminal, and the exact factory run and start must still be active and
unpriced. The session must still own one bound guest, zero `AgentTurn` rows, one
claimed and unconsumed sequence-one `PendingMessage`, and one matching project
permit in `running` or `uncertain`. The local reader also rejects a newer run,
a replacement guest or owner, changed `pending_seq` or dispatch count, partial
progress, a receipt with a committed body, another turn, or an unrelated
cleanup claim.

For a present guest, Ember must return the producer's complete `SessionView`
identity: the same session, a completed invoke, a positive and unchanged
`turn_seq`, unchanged generation and invoke timestamps, and a healthy,
non-draining owning node. The supervisor samples that exact evidence twice,
strictly more than the node's own `turn_timeout_seconds` apart. Fresh progress,
drain, replacement generation, malformed data, wrong identity, timeout, 403 or
5xx breaks or retains the proof and a later tick starts a new window. A 404 or
410 is authoritative absence only after it survives that same bounded sampling
window. One missing read is never cessation proof.

After the bound matures, the supervisor commits an exact cleanup fence before
issuing a conditional destroy for the observed generation and invoke. The
fence blocks claim release, error completion, progress and turn delivery, and
settlement rechecks the pending message, permit, zero-turn condition, receipt,
guest and factory ownership under the same lock order. A result committed
before the fence wins. A stale synchronous response or callback after the fence
cannot write a turn, settle capacity or affect a replacement attempt. Repeated
ticks reuse the same identity and may issue at most two conditional destroy
requests, at least five minutes apart. Exhausting that durable request budget
records one intervention-required audit instead of polling DELETE forever. A
control-plane lookup outage that holds the fence for two minutes likewise
records one intervention-required audit while retaining every row. Repeated
settlement does not add another result, notification, stop request or retry.

If fresh remote progress or a changed remote identity invalidates a held fence,
the supervisor retires only its exact cleanup claim. It also reopens the exact
uncommitted result receipt under the receipt row lock, capped by the receipt's
retention deadline, so the still-running invocation can deliver normally.

Successful settlement marks the run, start and session failed in one
transaction, consumes the pending message, settles the permit as
`delivery_error`, and clears only the exact ceased guest binding. Its
`cost_usd=None` is deliberate: a bound guest with completed invoke evidence may
have spent money, so the reserved ceiling remains charged. Only the separate
never-bound proofs may use measured zero and `no_model_post` accounting.

Do not destroy the guest before collecting this proof. Destroying first merely
removes the current binding or record and cannot establish that no guest ever
ran. Do not apply the SQL-shaped snippets from incident notes directly to
production. They omit the run/start consistency, receipt, ownership and race
fences above. If staged supervision cannot establish every prerequisite, leave
all rows intact and use the live-validation checklist on #6559 rather than a
blind database edit.

##### Live validation evidence

Each #6559 checklist item needs read-only evidence from a disposable factory
node. The switch has no per-node scope: it is one process-wide environment
variable on the monolith Deployment, so enabling it in `values-gke.yaml` arms
the proof for every bound zero-turn attempt the hub supervises. Before enabling
it, confirm read-only that the disposable node's attempt is the only one in
this shape (a terminal workflow, an active unpriced run, a bound guest, zero
`AgentTurn` rows, one claimed sequence-one `PendingMessage` and a `running` or
`uncertain` project permit). Record the task, node key, attempt, local session
id and guest id first. Before and after each step, record the
exact `AgentSession` (`status`, `ember_session_id`, `guest_cleanup_id`), its
`AgentTurn` and `PendingMessage` rows, the `AgentCapacityReservation` permit
(`state`, `outcome`), the node run and `FactoryStart` (`status`, `cost_usd`), and
the task's supervision audits (`stop_events` in
`factory_controls.task_snapshot`). Never destroy the guest before collecting the
proof, never apply SQL to production, and roll back by setting
`agents.sessions.boundZeroTurnSettlementEnabled` back to false in
`projects/monolith/deploy/values-gke.yaml`. Tests below are in
`factory_conductor_test.py` unless noted and give hermetic cover for the same
case; each item still needs its live record.

1. Strictly longer than `turn_timeout_seconds`. Record the node's pinned
   `turn_timeout_seconds`, the `bound_zero_turn_observation` (its `evidence`,
   `first_observed_at`, `observed_at`) and the `bound_zero_turn_fence` (its
   `evidence`, `first_observed_at`, `observed_at`, `held_seconds`). A
   completed-invoke run writes one observation and persists nothing for
   unchanged later samples, so judge the window on the fence record: its
   `observed_at` minus its `first_observed_at` must exceed that node's
   `turn_timeout_seconds`, and its `evidence` must equal the observation's.
   `held_seconds` is truncated to whole seconds, so a valid fence can show it
   equal to `turn_timeout_seconds`. Test:
   `test_bound_zero_turn_settlement_waits_strictly_past_timeout_and_is_atomic`.
2. Slow invoke, newer `turn_seq` or invoke timestamp, replacement generation,
   draining. Record the sampled `SessionView` fields (`session_id`, `state`,
   `turn_seq`, `generation`, `invoke_started_at`, `last_invoke_at`,
   `updated_at`, `node.health`, `node.draining`) on each tick, the
   `bound_zero_turn_reset` reason, and confirm no fence, no new `AgentTurn`, one
   dispatch and the permit state unchanged from its starting state. Tests:
   `test_bound_zero_turn_slow_live_invoke_never_starts_proof`,
   `test_bound_zero_turn_progress_or_identity_change_restarts_proof`,
   `test_bound_zero_turn_claim_heartbeat_restarts_window_and_fence_stops_refresh`.
3. Only sustained 404/410 matures. For a timeout, 403, 5xx, malformed payload
   and wrong session id, record the `bound_zero_turn_reset` reason, that every
   row above is unchanged, and that a later healthy tick opens a new
   observation run. For absence, record at least three
   `authoritative_absence` observations, no two more than ten minutes apart,
   spanning more than `turn_timeout_seconds`. Tests:
   `test_bound_zero_turn_transient_lookup_failures_retain_every_row`,
   `test_bound_zero_turn_live_proof_restarts_after_transient_lookup_failure`,
   `test_bound_zero_turn_lookup_failure_breaks_the_absence_window`,
   `test_bound_zero_turn_absence_gap_restarts_sampling`, and the
   `test_get_session_*` status mapping in `execution/transport_test.py`.
4. Late synchronous delivery and late receipt callback. Record the
   `AgentResultReceipt` and `AgentTurn` rows and the fence before and after each
   late write. A result committed before the fence must settle normally; after
   the fence, no turn is written and permit state is unchanged. Tests:
   `test_committed_synchronous_result_wins_before_bound_zero_turn_fence`,
   `test_committed_receipt_wins_before_bound_zero_turn_fence`,
   `test_bound_zero_turn_fence_rejects_late_result_and_release`,
   `test_bound_zero_turn_fence_rejects_late_receipt_callback`,
   `test_bound_zero_turn_remote_progress_reopens_fenced_receipt`.
5. Atomic settlement. Record one `stop_settled` audit, the run, start and
   session `failed`, no `PendingMessage`, the permit `settled` with outcome
   `delivery_error`, `cost_usd` null on both run and start, and the task's
   attempts and budget unchanged apart from that attempt. Tests:
   `test_bound_zero_turn_settlement_waits_strictly_past_timeout_and_is_atomic`,
   `test_bound_zero_turn_settlement_revalidates_local_ownership`.
6. Exact-guest release and no duplicates. Record the `bound_zero_turn_request`
   count (at most two) and its `precondition`, the cleared
   `ember_session_id` and `guest_cleanup_id` on only that session, and that
   later ticks add no audit, request, notification or retry. Any
   `intervention_required` `stop_observation` with reason
   `bound_zero_turn_destroy_exhausted` or
   `bound_zero_turn_fenced_lookup_unavailable` appears once. Tests:
   `test_bound_zero_turn_destroy_requests_are_durably_capped`,
   `test_bound_zero_turn_fenced_lookup_outage_raises_one_liveness_alarm`,
   `test_bound_zero_turn_completion_refusal_releases_fence`,
   `test_bound_zero_turn_release_can_mature_and_settle_a_new_fence`.

The in-process operator repair is
`factory.orchestration.factory_controls.settle_lost_attempt(task_id, node_key,
attempt, actor)`. Before calling it, an operator must confirm the exact node's
DBOS workflow is terminal. The function has no DBOS handle and cannot perform
that check itself. It then locks the factory control row and accepts only an
active, unpriced run with no newer attempt and the exact pinned workflow and
session ownership. The execution proof is re-read in the settlement
transaction, so a late guest binding, a new dispatcher, changed ownership, or
new execution evidence refuses the repair. Repeating the call after success
also refuses because the attempt is already terminal. This repairs one attempt
without cancelling or re-admitting the receipt, and it is deliberately
available while `factoryLostBeforeGuestSettlementEnabled` is off. It is an
operator action, not an HTTP endpoint.

An attempt whose workflow died mid-way has no session recorded on its run,
because `record_dispatch` binds one only at completion. The reconciler resolves
it by the deterministic `local_session_id`, `factory:<task>:<node>:<attempt>`,
under the same ownership checks `reconcile_completed_node` applies, and binds
it, so supervision can start. A repeated observation of unknown execution
records nothing: the first uncertain outcome stands until reconciliation makes
it terminal.

An aged reserved start can also be stranded before its DBOS workflow is created.
A successful lookup of that exact workflow returning absent is recorded separately
from a missing or malformed status and from a lookup error. The sessionless-start
sweeper applies the same locked proof as for a terminal failed workflow: the pinned
turn timeout has elapsed, and the exact admitted run and reserved start have no
session, deterministic session identity, permit, receipt, cost, outcome or newer
attempt. It atomically fails both ledgers at zero cost with `no_model_post` and
records `workflow_absent` in the audit proof. A delayed workflow must acquire the
same start guard and graph binding lock before creating its session; the terminal
run then refuses creation. Normal retry and funding reconciliation retain their
existing policy, deadlines and refusal bounds. An unavailable lookup, existing
execution evidence or a live workflow keeps the reservation.

Missing provider usage consumes the entire reserved ceiling. This is
conservative admission accounting, not an interruptible dollar cap on a running
provider turn. Observed overruns prevent further admission. The exception is an
attempt whose own evidence proves it never reached a model POST, an
`invocation_phase` of `never_dispatched`, `not_invoked` or `lost_before_guest`
recorded on the outcome or on the typed proof attached under that phase: it
books at nothing on the `no_model_post` basis, in the graph and on the start
row alike, because charging it the ceiling retired its node on the first
failure (#6045). An attempt that may have reached
the model with an unknown cost, `guest_cessation_confirmed` after dispatch
among them, stays conservative and keeps its reservation.

A retry interrupted by a drain is not a first-dispatch failure. The separate
`interrupted_then_not_invoked` proof requires an authenticated native drain
response for every preceding dispatch, in order, followed by an exact settled
`not_invoked` permit and matching failed turn. Missing receipts, overlapping
execution, captured final responses, pending work, aliases, and observation or
cleanup ownership keep the attempt uncertain. A complete proof fails the graph
attempt without changing native execution history or guest bindings. Earlier
spend remains unknown and consumes the reserved ceiling and attempt allowance;
only the unresolved hold is released so normal planning can proceed within the
existing task policy and deadline.

A terminal workflow can also leave an unclaimed drain continuation behind.
`interrupted_continuation_retired` requires the exact native continuation grant
and authenticated, chronologically ordered drain responses for every physical
dispatch, including the last one. The conductor rechecks this proof under its
control lock with the execution locks, settles the permit, consumes only that
grant, and records the failed graph/start and audit in one transaction. The
session becomes failed while its guest binding, native turns, receipts, policy,
and deadline remain intact. Unknown spend consumes the reserved ceiling and
attempt allowance. A live workflow, changed ownership, missing native response,
or pending observer prevents settlement. This proves the model turns ended;
it does not claim the guest was destroyed and does not issue a remote stop.

An initial guest allocation can outlive a cancelled client request without an
invoke timestamp. Its later `evicted/idle_ttl` record proves cessation only when
the exact guest was created during the failed dispatch, the terminal update is
after the recorded failure, generation and turn sequence are explicitly zero,
and invocation, interruption, and stop fields are explicitly empty. Missing or
conflicting fields, an older allocation, or a saved stop identity refuse this
proof. The ordinary supervisor still revalidates local ownership and records
`terminal_initial_guest` with the terminal evidence. It retains unknown spend
at the reserved ceiling; guest cessation is not evidence of zero provider cost.

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

The same loop owns landing recovery. A delivered PR with a merge conflict or
confirmed queue ejection returns to the same task for bounded assessment and
correction. Conflict workers rebase onto the PR base; other ejections require
reading queue and check evidence before choosing a retry, rebase, code repair,
or escalation. Every round ends in an independent review of the resulting head.
`landing_recovery_round` records the request and graph round. Exhausting the
ordinary correction bound returns the evidence to the conductor's existing
assessment, funding, and escalation path.

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

The pull request body follows the task's recorded gate state. With no recorded
live checks, it must contain `Closes #<issue>` regardless of the node's own
assessment of operational acceptance. A node that believes live acceptance
remains reports it in its artifact summary for the planner to record a gate.
With recorded live checks, the body must contain `Refs #<issue>`, a line starting
`Conductor rescope:` stating the rescope, and the outstanding checklist, with
no closing keyword for that issue. The conductor pins the current rule at
dispatch for implement, correct and integrate nodes. Independent review nodes
check the body at the exact head and return `changes_requested` on a mismatch.
Stored node boundaries carry no closing rule; a gate recorded after insertion
reaches the next dispatch, while an admitted attempt retains its immutable pin.
The planner uses the same rule. The completion gate reads the body back from
GitHub and refuses a delivery without a closing keyword as
`pr_missing_close_keyword` when no live checks are recorded. The gate
accepts every keyword GitHub acts on, `closes`, `fixes` and `resolves` in all
their forms, because refusing a body that says `Fixes #123` would fail a
delivery that does close its issue, and all three reference forms GitHub
honours: `#123`, `owner/repo#123` and the full issue URL. Only this task's own
issue in this task's own repository counts.

### Turn observations survive queue deletion

`AgentTurn.usage_json["observation"]` stores `turn-observation/1`: the requested
model, queue and dispatch timestamps, dispatch count, and timing for one
physical dispatch. Queue and recording timestamps are monolith pod wall time
(`monolith_wall`); the dispatch timestamp is the database server's
transaction-start time (`database_transaction_start`), so queue wait mixes
both clocks (`monolith_wall+database_transaction_start`) and reads null when
clock skew inverts the order. After a drain or brick retry, queue wait spans
enqueue to the latest dispatch, so it includes the earlier dispatch's
runtime. Executor elapsed
time uses `executor_monotonic` around the single transport delivery await.
Runtime duration is `guest_reported`, accepted only as an integer from zero
to less than seven days. The block records these clock labels explicitly.

Null means unavailable. Receipt adoption has no executor measurement;
executor-failure error rows, unknown-invocation and pre-dispatch cancellation
rows have no runtime or executor measurement. Provider retries remain null because current adapter
results do not report them. Dispatch count tracks executor dispatches of the
pending sequence; the factory node attempt and CLI loop count remain separate.
Drain continuations retain each prior observation under `drain_continuations`
and write a fresh observation without adding elapsed times. Guest usage cannot
override the server-owned block.
Completed-receipt reconciliation also writes this block. Its pending row is
already gone, so queue identity and executor timing are null; the final native
receipt supplies runtime duration. The original row survives in
`factory_receipt_recovery.previous_turn`. A drained response-loss hold writes
an observation before releasing its claim and retains the pending row for
the next dispatch. The not-invoked error path snapshots its pending row before
deletion. Claim heartbeats do not write turns.

`create_turn` remains a low-level writer: direct callers must supply their
observation. Duplicate-terminal cleanup preserves the existing turn without
rewriting it, including legacy rows without observations.
`settle_interrupted_factory_continuation` retains the interrupted turn while
deleting its unused continuation, as does `settle_drained_lost_factory_attempt`.
`settle_stranded_factory_attempt` preserves existing turns while consuming
pending rows. `settle_never_dispatched_factory_attempt` and
`settle_bound_zero_turn_factory_attempt` consume pending rows without creating
turns. Those paths retain their existing settlement semantics and do not
backfill observations. `delete_pending_message_sync` assumes a persisted turn.
The drainer's `destroy_drainer_session` bulk cleanup cancels queued messages
and deletes them without writing turns; no dispatch observation is created
for that lifecycle cleanup.
(see: /projects/monolith/factory/execution/store.py)
(see: /projects/monolith/factory/execution/mcp.py)
(see: /projects/monolith/factory/execution/reconciliation.py)

### Permit supervision boundaries

Capacity is released only by the explicit reconciliation owner after fresh authoritative
terminal proof for the exact guest, generation, session and turn. Uncertain
permit cessation settlement is behind
`agents.sessions.uncertainPermitSupervisionEnabled`, which defaults off. It
covers kg and project drainer permits, interactive permits, and factory-owned
project permits. When probe supervision is also enabled, it extends probes to
destroyed and no-guest proof. A bound guest requires exact control-plane
eviction or timestamped destruction after the failed turn, and a guest that
ceased without ever being stopped reports no stop precondition, so the factory
path takes its identity from the committed stop intent or from the observed
invocation. Both loops order that invocation against the failed turn rather
than against the dispatch, because the dispatch stamp is written when the
executor claims the turn and so lands before the guest is ever invoked. A
no-guest permit requires durable
claim evidence with no guest, binding or residual lineage evidence, and an
operator destroy no longer clears a binding under an unresolved outcome, so a
cleared binding cannot masquerade as one that never existed.

`factory.orchestration.drainer` holds the routine job on
`InvocationOutcomeUnknown`. Held jobs remain refused as `routine_job_held`
unless `FACTORY_RESERVATION_REVIEW_ENABLED` enables their reviewed path.
`agent.routine_reconciliation.read_reconciliation_state` accepts both `kg-drain`
and `qwen-drain`, subject to exact job, session, turn, workflow and permit proof;
other routine kinds refuse. Supervision reconciles an eligible ceased held job
and its permit in one transaction. It preserves applied-output evidence and
chooses retain, rearm or an explicitly reviewed stop through that owner.
The 2026-09-09 KG stall motivated this boundary (#5983); repository code does
not establish those historical permits' current state.

The shared settler preserves session bindings. Factory-owned sessions are
excluded and use the factory settlement path, including proof of no delivery
for attempts with no bound guest. Legacy swarm project sessions without a
routine job are not universally covered: active sessions fail the drainer
selector, while terminal unbound shapes must satisfy the narrower stale-unbound
proof. Prior lineage, unsupported turn history or contradictory execution
evidence still refuses settlement. A missing binding alone is insufficient.
(see: /projects/monolith/factory/execution/permit_supervision.py)
(see: /projects/monolith/agent/routine_reconciliation.py)

### Guest reuse cleanup

The fence now has an owner with a deadline at
both ends. The invoke coroutine's terminal path releases the exact receipt it
fenced, bound to the same session, receipt, guest, claim owner and dispatch
count identity the observed path checks, so a stale coroutine can neither
release a newer fence nor authorize destroying a newer guest, and then runs the
ordinary completion cleanup. That release is optional work on a completion
path, so a contended pool that answers `lock_not_available` inside its one
second lock timeout is retried a bounded number of times and then logged: it
never replaces the exception the turn was already raising, and never stands
between that turn and the destroy and unbind that follow it. The two cleanup
owners are the backstop for a crash between those steps, and for every session
that keeps its guest resident rather than destroying it at turn completion.
Both release a fence whose receipt is gone, whose body has arrived, or whose
acceptance window has closed. The drainer releases inside its own cleanup
transaction, so a refused cleanup rolls the release back with everything else
it was going to write. The workflow reaper cannot, because `begin_guest_cleanup`
refuses while any row bound to the guest carries a fence, so it releases first
and puts the fence back when no claim follows: a committed claim blocks
dispatch, receipt minting and rebinding in the fence's place, but between the
release and the claim nothing does, and a row left pending with neither would
let a queued follow-up claim a guest whose POST may still be streaming. What
none of them will touch is a
receipt still inside its acceptance window with nothing committed, because
until that body arrives the guest is the only path to the turn. The release
deliberately does not stamp `response_observed_at`: nothing observed a
response, and the lease backstop and factory reconciliation read that stamp as
evidence about the response itself, so the count of received receipts with no
observed response stays the honest health signal for how often the receipt is
winning the race.
(see: /projects/monolith/factory/execution/result_receipts.py)

### Landing

The operational health loop reports landing or observed-merged active work
waiting over 30 minutes for reconciliation or rollout. Health evaluation does
not settle, resume or otherwise repair that work.

Merge observation runs before review publication, including previously refused
deliveries. A bounded rotating scan revisits those receipts even with merging
disabled. A recorded merge must match the owned repository, base, branch and
approved head. Observation grants no new merge authority and preserves explicit
holds and pause fences.

For active work, `factory_merged_delivery.py` records `owned_pr_merged` when the
owned PR merged elsewhere. Existing invocations drain through normal accounting
before exact-head independent review and CI verification. Approved work enters
landing even with `auto_merge` off. Missing or rejected review escalates without
another planner turn or funding grant. A paused receipt with an observed owned
merge may finish verification, but cannot publish a review, recover or arm an
unmerged PR. Infrastructure rollout and outstanding functional acceptance remain
separate, including when another actor closed the issue.

#### Dependency queue evidence

`factory/dependency-evidence` is a trusted monolith check, staged off by default
with `factory.dependencyGate.enabled: false`. The conductor tick runs it
independently of guest execution, intake and merge landing. It publishes on
server-fetched open PR heads and merge-queue entry heads. GraphQL supplies the
entry's actual `baseCommit`, `headCommit` and PR `headRefOid`; the gate re-reads
the queue and PR after collecting evidence. Bounded, unavailable or malformed
discovery never produces a success. Publications, refusals and discovery
failures are audited. Disabled and token-missing ticks return without an audit,
database session or control lock.

The publisher uses only `FACTORY_DEPENDENCY_GATE_TOKEN`, an App installation
token supplied to monolith through the 1Password Operator and the trusted
broker path. Both reads and check writes use that identity. No token fallback
or chart Secret binding is included. A missing token publishes nothing. These
configuration skips deliberately have no per-tick audit.

Every enabled tick revalidates current evidence before reading existing check
runs with the dedicated token. `FACTORY_DEPENDENCY_GATE_APP_ID` identifies the
App whose latest completed conclusion may suppress an unchanged publication.
Other Apps' checks grant no publication authority. Missing or invalid App IDs,
incomplete or malformed inventories, and failed check reads always publish.
A changed conclusion publishes, including success revocation and recovery.
Provision the numeric App ID alongside the token and verify both belong to the
integration pinned in the ruleset before activation.

Numeric `intake.dependency_pr_author_ids`, GitHub `user.type == Bot`, and
`renovate/` or `dependabot/` refs force review evidence. Refs and numeric IDs
grant no authority. Non-dependency human and factory issue PRs pass identity
validation. For dependency PRs, the latest receipt generation must be settled,
unpaused and uncancelled, with both safe independent assessment artifacts on
the exact PR head. The approved digest is checked before comparing evidence.
The queued base/head dependency comparison, file inventory with blob SHAs and
all open alerts must equal the reviewed content. Only comparison commit IDs
may differ. GitHub's compare file cap is a refusal, even at exactly the cap;
a successful empty dependency comparison records a coverage limitation. Both
independent artifacts must enumerate every changed filename in `reviewed_files`
and supply concrete supplemental provenance/advisory evidence in
`evidence_coverage`. Unavailable, null, malformed or truncated reads still refuse.
Both independent artifacts carry the same complete base/head `advisory_sources`
inventories, bound to the reviewed commits. Each side covers every corresponding
changed file, using the previous filename for renames on the base and excluding
added/removed files from the side where they do not exist. Each component binds
a stable occurrence key, immutable artifact SHA256, exposure SHA256, exact OSV
package/ecosystem/version or Git commit query, file associations and complete
response hash. Reviewers establish the full dependency/SBOM closure, occurrence
identity, exposure derivation and feed applicability; agreement and hashes alone
do not prove completeness. Empty results for unknown packages are not proof.
The bound is 100 components per side.

The server freshly queries both sides at completion and each publication with
no credentials, redirects or caller-selected endpoint. It retains all fields of
every advisory, normalizes absent findings to an empty list and sorts records by
ID before hashing canonical JSON. Changed content invalidates approval. Under
the existing non-regression policy, partial remediation can pass when unrelated
findings remain only on exactly unchanged components: occurrence, artifact,
exposure, query/version, file associations and advisory content must match.
Added vulnerable versions, copies or new exposure invalidate approval even when
another finding is removed. Residual findings require the actual PR or queue
base to equal the independently reviewed base. A moved base invalidates approval
before waiting for CI or OSV; identical candidate blobs do not prove unchanged
baseline exposure or findings. Clean candidates retain equivalent-content queue
comparison. No waiver is granted. Unavailable, oversized,
malformed, incomplete or paginated responses refuse without claiming positive
change. Other sources require an adapter or remain refused; prose cannot
substitute for revalidation.

Dependency success waits for exact-head `pr-checks`. Each head is evaluated
immediately before publication, including a final durable-approval read, with
at most 60 seconds from collection start to the publication request. A slow
check-inventory read also expires that result, even if an earlier success
exists. This is a collection bound, not a lease: GitHub's Checks API has no
expiry field and publisher failure cannot retract a stored success. Combined queue prefixes
containing dependency PRs fail closed instead of attributing aggregated changes
to one receipt.

Changed, moved, unsafe or invalid approval evidence records
`dependency_approval_invalidated`. That receipt cannot pass again even if the
old evidence returns. Fresh adversarial review requires the existing authorized
generation mechanism. Unavailable, incomplete or malformed fresh evidence
refuses this tick without permanently invalidating the approved generation.
Success still requires a fresh positive match after the API recovers. This
publisher neither changes policy nor starts work.
Dependency landing still refuses `dependency_auto_merge_disabled`
unconditionally, and `intake.dependency_pr_authors` remains empty by default.
The staged Renovate policy disables native automerge globally and in its rules;
the weekly maintenance helper also requires an explicit default-off flag.
#7186 owns those switches and schedule activation after the trusted App-bound
context and remaining acceptance requirements are enforced. Inactive preparation
can ship separately. The factory does not arm them.

Live checks outstanding for #6799, outside this repository-only slice:

1. Provision the dedicated App installation token to trusted monolith, with
   repository, dependency graph, Dependabot alert and commit-status reads plus checks write.
   Keep the token out of guests and untrusted same-repository CI.
2. Enable the flag separately and canary `factory/dependency-evidence` on a
   real PR head and a merge-group commit. Verify the actual entry base and
   complete server evidence, including a refused combined dependency group.
3. Require this context in ruleset 9180009, pinned to the numeric App integration
   ID, alongside Linux `pr-checks`. Audit bypass and any-App sources.
4. Verify a stale or advisory-changed queued dependency PR is refused and needs
   a new authorized generation. Polling cannot guarantee freshness if an advisory
   arrives between success and merge. This is point-in-time verification of
   the exact queue commit. Missing token/API failures also cannot revoke a
   check already held by GitHub. Canary the implemented boundary and obtain
   Joe's explicit acceptance of that residual race before activation; an expiry
   service is not an established repository requirement.
5. Verify an unapproved Renovate PR with auto-merge already armed cannot merge.
   The staged Renovate policy requests auto-merge only after its separate activation; it does not disarm existing PRs.
6. Resolve and verify #7005's same-repository secret boundary. BuildBuddy
   trusts same-repository events before reading PR-controlled YAML; a shell
   guard or action split cannot remove that authority. Isolate PR/queue
   execution from publication credentials and main's saved snapshots through
   platform controls, then prove main publication and required CI still work.
   No such provider configuration or credential change is made here.
7. Create and verify a dedicated Renovate author identity before allowlisting
   any author. Historical maintenance PRs use `jomcgi`; never allowlist the
   human account to activate this lane.

#### Landing recovery authority

A request-bound engine edit is the normal proof that a
bounded correction round exists. Plans written before that marker require a
successful source-writing dependent above the request's run floor with typed
evidence for the same pull request and exact written head. Other dependents do
not block the engine from appending its correction and independent re-review,
and armed or run-bearing nodes remain immutable history. Completion still
requires the paired recovery audit plus a fresh, independent, approving review
of the delivered pull request head above that floor; neither the audit, an old
approval, nor a `changes_requested` result is sufficient.

The landing-recovery deadline is a fixed cap, distinct from a renewable funding
lease. Snapshots and start permission identify its expiry as
`landing_recovery_deadline`. Funding cannot buy another reassessment or extend
that cap, including when it expires during evidence reads or before a queued
review starts. Completed paid reviews still settle once with their accounting
intact, but cannot issue an ineffective grant. The receipt and its lane slot
remain held for an operator recovery decision; this fence does not resume,
re-admit, cancel, or release the task. Ordinary funding leases retain their
existing renewal path.

#### Review publisher

The landing path contains a trusted `factory/review` check publisher. It is off
by default (`factory.reviewPublish.enabled: false`) and changes no behavior
while off. When enabled, it loads the task's durable receipt, admission policy
version, settlement PR and approved head, and latest assigned `review_*` run.
It accepts only a successfully validated approval from a session distinct from
every implementation, integration and correction session. A fresh GitHub read
must show the open, non-draft PR in the policy repository, on the exact
operator-authorized task branch and base branch, at that approved head. Missing,
failed, invalid, stale, self-authored, moved or superseded evidence fails closed.
A later valid `changes_requested` review of the same SHA publishes a failing
check to invalidate an earlier success.

The publisher uses only `FACTORY_REVIEW_PUBLISHER_TOKEN`, supplied through an
injected provider. It never falls back to `GITHUB_API_TOKEN`. A missing token
publishes nothing, audits `review_publish_skipped`, and prevents landing from
arming that pull request. The chart intentionally wires no Secret in this
change.

Activation still requires operator work outside this repository-only slice:

1. Supply `FACTORY_REVIEW_PUBLISHER_TOKEN` only to the trusted monolith caller.
   The tokenbroker GitHub App lane was removed in #7060; any replacement GitHub
   identity belongs to #6442 or a successor issue. Keep the token away from
   guests, implementers and the shared noded identity.
2. Canary a real check, then require `factory/review` on `main`, pinned to the
   Bosun numeric App ID alongside existing Linux CI. Do not use an any-App
   source or add an agent to a bypass list.
3. Verify an unreviewed or moved head cannot merge and a later rejecting review
   invalidates the same-SHA success before enabling merge behavior.
4. Activate merging separately. This change does not provision credentials,
   alter branch protection, enable the publisher in a deployment, or enable a
   merge queue.

Merge landing is off unless the policy sets `auto_merge` to `true`, and only
the value `true` counts: a policy written before the flag existed, or one
carrying anything else, lands nothing. Review publication may be canaried with
merge landing still off. Merge landing is the first factory step that writes
pull request and issue state, so the process needs a GitHub token with those
permissions before the merge flag is worth turning on.

With the flag on, exact-head delivery approval records `delivery_ready` and
moves the receipt and task to `landing`, with no settlement timestamp. This
releases the guest execution slot but retains issue and branch ownership across
generation changes. The review publisher accepts this pending state. The PR
has its merge armed through the GitHub auto-merge mutation with the rebase
method, the equivalent of `gh pr merge --auto --rebase`, and the lane audits
`merge_armed`. Landing first checks that the pull request is still at the exact
approved head and has no computed merge conflict. Exactly one factory pull
request is armed at a time, because
this repository merges through the GitHub merge queue and an ejection cascades
across every candidate behind the one that failed. The holder is read from the
lane's own audits and from GitHub: before arming anything, up to ten pages
of 25 open pull requests are listed and any pull request on a `factory/` branch with
auto-merge or a merge queue entry already set counts, so a pull request an operator armed by hand is
not raced. A holder check that cannot be read or exhausts ten full 25-row pages arms nothing,
because not knowing
is not a licence. Every waiting delivery audits `merge_deferred`, once per
blocking pull request, and is armed on a later tick.

Selection is on landing state, never on recency: every non-advisory `landing`
receipt and legacy `succeeded` delivery whose landing has not reached a terminal
audit, oldest first. New pending deliveries never age out. Terminal
is `merge_arm_refused`, which hands the pull request to a human, or
`issue_closed`, which is the last step of a successful landing. Taking the
newest receipts of any class instead let a burst of advisory settlements push an
armed but unmerged delivery out of the batch, which left it never observed and
the holder reading as absent. A delivery the lane has never touched is skipped
once it is a week old, so turning the flag on does not stampede over history,
but anything the lane has armed is followed to a terminal state whatever its
age.

The same reconcile tick owns PR retirement independently of `auto_merge`.
When a receipt settles `escalated`, `cancelled`, or `failed`, including the
deadline-expired escalation, its open same-repository `factory/` PR is
converted to draft. One marker-fenced comment names the receipt, settlement
reason, and the next action: re-admission adopts that branch. A succeeded task
is untouched. Before the comment and again before the draft mutation, the
server re-reads the PR and checks active branch ownership. If a successor task
has adopted it, that task owns readiness and the old settlement does nothing.

The paced sweep on that tick examines at most 20 open PRs from a persisted page
cursor. It never mutates a non-`factory/` head. An unowned factory PR is closed only
when every closing-keyword reference is closed or has another open factory PR
whose exact branch is owned by a running receipt. A historical reference such as
`closed #5851` cannot retire delivery for another open, uncovered objective.
This requirement is checked again immediately before the close. The comment names the
survivor when one exists and otherwise says explicitly that the issue is
already closed. The survivor is not modified. A prepared audit, a hidden
comment marker, fresh PR and owner reads, and the `factory_pr_retired` audit
make partial GitHub failures and repeated ticks safe. Cursor progress is
durable, so 20 permanently live older PRs cannot hide later stale candidates.

This is configured repository behavior, not an operational rollout claim. The
2026-09-19 observation of duplicate pairs 6198/6070, 6199/6075, 6209/6082,
and 6215/6078 is the replay fixture for the gap. This change does not assert
that a deployed reconciler has run the sweep against GitHub; rollout and its
first observed audits remain operational evidence.

Later ticks observe the armed pull request:

| What GitHub shows | What landing does |
|---|---|
| merged | audits `merged`, then closes the issue |
| closed, not merged | audits `merge_arm_refused` and stops |
| open, head moved off the armed SHA | turns auto-merge back off, audits `merge_arm_refused` with `head_moved` |
| open, computed merge conflict | disables auto-merge, then requests bounded recovery |
| open, neither auto-merge nor queue membership | requests assessment of the ejection |
| open, still armed or queued | waits |

Queue membership is checked independently of auto-merge. GitHub clearing
`autoMergeRequest` on queue admission is not an ejection. Failed or malformed
membership reads retain the slot.

Recovery preserves the same task, graph, PR, spent turns, and dollar accounting.
It admits at most two recovery episodes per task, each with a fresh one-hour
deadline, and respects current delivery capacity, task pauses, cancellation,
and unresolved execution. It does not reset the correction-round or spending
limits; any further funding uses the existing conductor assessment. A recovered
settlement starts a fresh landing epoch, and arming requires its approved SHA
to match GitHub. Exhausted recovery is left for a human with a warning.

One historical `merge_conflict` or `ejected_from_merge_queue` refusal is
reassessed per tick. Reopening supersedes that refusal without deleting history.
Closed PRs, changed heads, active queue entries, and operator refusals are not
blindly retried. A request that cannot acquire delivery capacity remains eligible
for a later tick; it does not bypass the concurrency limit.

A mutation GitHub refuses audits `merge_arm_refused` and is not retried; a
GitHub read or write that fails audits `landing_error` by exception type and
status, never by response body, at most once an hour per task, and the next tick
retries. `merge_armed` and `merge_ejected` are counted rather than fenced,
because a delivery can be armed, ejected and armed again; every other step
writes one row per task and that row is its fence.

A merge is not task success. Landing verifies a completed chart publication
receipt on main whose source includes the merged commit, plus successful
`pr-checks` on that source. No-op publication receipts prove reused versions.
Rollout scope follows the merged PR's deploy surface: registry chart versions
moved since the merge, git paths and their kustomize surface, and `$values` files
(#6660). Chart scope uses the publication with the earliest proven source
containing the merge; later unrelated chart bumps do not widen it. Chart
deployment proof uses the newest proven source, while git revisions need only
contain the delivery's source. Source ancestry orders receipts; the receipt
commit bounds the history search even when a publisher finishes late. Unknown
kustomize keys conservatively include the Application. Workload reads begin only
after scope selection, and unreadable evidence fails closed. A PR reaching no
live Application records `no_live_application` with its publication receipt's
successful `pr-checks` render check.

For scoped Applications, the verifier checks actual Argo sync revisions against
the receipt and source ancestry, Healthy/Synced status, the compared source
specification, workload generations, ready replica counts, and running pod images.
Repository-built images must be digest pinned; external tagged images must match
the workload specification.
Pod evidence excludes terminal and deleting remnants while requiring the desired
number of active, ready replicas running the requested images. Unrelated
Applications do not block scoped acceptance. This verification does not claim
functional acceptance.

Unknown, missing, paginated or unavailable observations fail closed. Each task
records at most one pending observation per minute; mutable reads are shared
for at most 30 seconds, including failed reads that remain failures rather than
empty successful inventories. This prevents a cluster timeout from repeating
for every pending delivery in one tick. A wait appears in status and the daily
digest at 30 minutes and pages once at 60 minutes. Landing keeps polling each
minute. Immutable SHA reads have a bounded cache. Reads do not mutate Argo,
restart guests, or grant credentials. A durable
`rollout_verified` audit binds the approved PR head, merge, publication and
source SHAs to observed application revisions and workload images. Only that
trusted matching proof allows `landing` to settle `succeeded`. Disabling
`auto_merge` cannot bypass the pending gate. Reconciliation safely resumes
between verification, settlement and issue closure. Legacy already-settled
deliveries also require this proof before the factory closes their issue.
The merge-time audit retains a null rollout field because proof comes later
in the separate `rollout_verified` event.

### Outcome evidence: CI at merge, reverts after it

The manual read-only cohort report is described in
[outcomes.md](../outcomes.md). It counts all attempts through an explicit as-of
bound, retains unknown/pending evidence, separates priced turns from settled and
reserved ledger costs, includes failed terminal tasks in elapsed-time ratios,
and keeps initiating-model task cohorts separate from role/model contributions.
Model-bench #6702 covers cheaper-model counterfactuals; pairwise rating #6700 is
closed completed. Neither issue state establishes live factory acceptance.

Two record-only audits make a factory merge measurable as an outcome (merged,
CI green, not reverted within seven days; #6716). Neither gates landing.

`merge_ci` is written once, right after `merged`, by
`factory_landing._record_ci`. It reads the merged head's commit statuses and
check runs and folds them into one conclusion: any failed state fails, all
passed (`success`, `neutral`, `skipped`) passes, and anything else is
`pending`. `ejections` is the task's `merge_ejected` count in the current
landing epoch. A failed read still writes the row, as `unknown` with the
exception type, because a merged delivery leaves the landing batch once its
issue closes and is not asked again.

```json
{"pr_number": 6704, "head_sha": "<40 hex>", "ejections": 0,
 "checks": {"pr-checks": "success"}, "conclusion": "success"}
{"pr_number": 6704, "head_sha": "<40 hex>", "ejections": 0,
 "conclusion": "unknown", "error": "ConnectError"}
```

`factory_reverts.revert_tick` runs after landing on every reconcile tick,
throttled to one sweep per 15 minutes per process. It takes factory `merged`
audits from the last nine days (a seven-day window plus two days of grace)
that have no terminal row yet, reads the base branch history since the oldest
of them (at most ten pages of 50 commits), and writes one terminal row per
task:

- `reverted`, when a commit inside the window says `This reverts commit
  <merge_commit_sha>`, says `Reverts owner/repo#N`, or has a `Revert` subject
  citing `#N`;
- `revert_window_closed`, once seven days pass with none, and only when the
  history read reached back to the oldest merge being judged.

```json
{"pr_number": 6704, "merge_commit_sha": "<40 hex>", "revert_sha": "<40 hex>",
 "revert_committed_at": "2026-10-04T09:00:00Z", "detected_at": "<iso>"}
{"pr_number": 6704, "merge_commit_sha": "<40 hex>", "window_days": 7,
 "checked_at": "<iso>"}
```

Merges older than the grace, including every merge from before the sweep
existed, are never judged and read as unknown. With rebase merges the merge
commit is only a pull request's last rebased commit, so a partial revert of an
earlier commit in the same pull request is not detected.

### Explicit review-only recovery

Explicit review-only recovery is a separate paid episode on the escalated
receipt, never a resumed DAG or a renewed landing-recovery deadline. The
operator supplies an idempotency key, the existing PR and exact head, a new
bounded deadline, and cumulative dollar/work-turn ceilings no higher than
existing authority. The start ledger includes all objective history and
unresolved reservations. Every explicitly requested retry consumes a new
reservation and work turn. No planner, funding judge, correction or implicit
retry runs. Missing money or turns returns the precise deficit.

Recovery sends bounded, immutable base/head evidence
to a tool-free reviewer through one fixed provider endpoint. It creates no
guest/session and exposes no shell, Git, MCP, HTTP tools or GitHub credential
to the model. Only a dedicated recovery API key and explicitly configured
reviewer alias are accepted, with no shared-key, model or provider fallback.
The alias must belong to the receipt's reviewer pool; judgment-class recovery
is unavailable on this OpenAI-only adapter. Fixed model identity and list
pricing come from shared pricing. Unavailable configuration fails closed.

The maximum input/output charge is reserved under the factory control lock.
This first adapter conservatively commits that entire ceiling, also on a
successful response; it does not claim a provider-reported actual cost.
Uncertain calls retain the reservation and block retries. A proven no-post
fence releases money. The bounded provider call is serialized with stop/hold
under the same lock, for at most 120 seconds; client cancellation cannot prove
remote provider cessation, so its reservation remains charged. Late results
cannot approve. Exact-head CI and draft/queue state are rechecked before and
after review. Head or base changes invalidate the result.

The append-only episode audit supplies a sticky review-only hold independently
of global auto-merge. Ordinary starts, funding, re-admission, delivery settlement
and merge arming refuse it. Success records `awaiting_landing_approval` while
the receipt remains escalated, with the old graph, deadline and audit intact.
This control has no release/landing operation. Deployment, dedicated credential
provisioning, live review spending and eventual landing each require their
separate operator decision. No deployment values activate this path.

(see: /projects/monolith/factory/orchestration/review_recovery.py)

### A delivery pause is a decision request

The delivery planner's fifth action is `pause`, and it now leaves the lane
rather than sitting in it. A pause carries the one `question` a person must
answer and the same two to four `options` a refine escalation carries, with the
recommendation first, and the prompt asks for concrete ones: rescope to a named
surface, close as stale, split, defer, continue with a stated assumption.

Option one has to have effect `agent-ready`, which is the one that carries the
work on. That is stricter than the refine rule, where the recommendation may be
any of the four, and it is stricter for one reason: `resume_task` applies
option one without showing the operator the card, so a pause recommending a
close would turn pressing resume into closing the issue. The planner may still
offer close, split, defer and hold; it may not recommend them from inside a
task that has a branch and usually a pull request open. A pause missing its
question, its options, or that first effect is refused as decision feedback
with `pause_without_question`, `pause_options_invalid` or
`pause_recommendation_not_continue`, so the planner repairs it inside the task
it already has instead of settling a card nobody can safely act on.

**Why.** A pause is a decision request, so it leaves the lane. It used to leave
the receipt `admitted` with `task_paused` set, which held a delivery slot and
its accounting until an operator resumed or cancelled by hand. Worse, resuming
replayed the same pause: the planner's context is the issue body captured at
admission plus graph evidence, so an answer posted as a comment never reached
it. On 2026-09-12 that happened twice in one day, on #3824 and #3832. So the
pause settles instead. The receipt goes to a new state, `escalated`: the slot
and every reservation are free, no node runs, the graph and the accounting stay
exactly where they are for a decision to be read against, and the question
reaches a person as a card with buttons.

Settling writes the `needs-human` label first and the decision card second. A
card posted onto an issue intake can still pick up is the one ordering that
lets the lane re-admit the work while somebody is reading the question. The
card names the question, the planner's reason, where the task got to, the
numbered options, and the branch and pull request the attempt left behind. Both
writes are fenced, the label by being idempotent and the card by a hidden
marker naming the task, because settlement is re-reached on every tick until it
takes. One Discord warn carries the escalations link. Intake skips an escalated
issue twice over: it carries `needs-human`, which the default exclusion list
drops, and the sweep also excludes any issue holding an escalated receipt, for
the operator who takes that label off while the decision is still open.

**Deciding re-admits with direction.** An escalated delivery is decided through
the same endpoint and the same escape group as a refine escalation. An option
whose effect is `agent-ready`, which is what continue, rescope and assume all
are, returns the receipt to `queued` under its own identity and records the
choice, its detail and the operator's note as `direction_json`. The next
admission mints a new task with an empty graph, and that task's first planner
round carries the direction in its context as an `operator_direction` section:
untrusted text and evidence, never authority, naming the previous task, its
branch and its pull request so the work is carried on rather than started
again. Later rounds do not repeat it, because the plan it shaped and the
decision feedback under that are already the record of it. Reading it audits
`operator_direction_read`.

`close`, `defer`, `split` and the escape group behave exactly as they do for a
refine escalation, and then settle the receipt `cancelled`. Cancelled rather
than succeeded, because nothing was delivered and a succeeded delivery receipt
sits in the `delivered` exclusion intake keeps for good, which would take the
issue off the lane on a decision that never said to. A receipt the lane could
not admit again, a generation the policy has moved past most often, is settled
the same way with `blocked_by` on the resolution: an answered card whose work
is scheduled nowhere is the stuck state this replaced.

The `chat` action works on a delivery escalation too. It posts `Operator asks:`
on the issue and re-admits the same way, with the note as the direction and no
option applied. It does not resolve the escalation, so the card stays live and
an option can still land on a receipt that is already queued: that answer
replaces the direction the chat left rather than re-queueing a second time, and
a terminal answer settles the receipt `cancelled`. The branch and the previous
task come off the stored direction there, because a re-queued receipt no longer
names a task of its own. `resume_task` on an escalated receipt applies the recommended
option, because the first option is the recommendation and resume is choosing
it without reading the card; `stop` settles every escalated receipt
`cancelled`, since the reconciler only visits active tasks and would otherwise
leave a card waiting on a decision for a lane that is shut. `pause_task` and
`resume_task` are unchanged for a task that is still running.

A re-admission is a whole new task with a fresh graph, a fresh allowance and a
fresh `task_budget_usd`, because the escalated attempt's spend is history and
the new task has to be able to plan and deliver inside its own envelope. So the
receipt carries `previous_task_ids`, and the board shows what those attempts
cost as `previous_spend` beside the current task's own accounting rather than
folded into it: `turns_used` and `committed_cost_usd` are measured against this
task's allowance, and adding a previous attempt's spend would read as a task
over budget before a node had run. Nothing caps how many times one issue may be
re-admitted, so that total is the number to watch.

The receipt holds one escalation document at a time. A task that escalates a
second time supersedes the first, moving it into `history` with its resolution,
because the same receipt is re-admitted under its own identity and keeping the
answered document would leave the page showing options written before the
question was answered.

### Automatic pause questions

Automatic safety pauses retain their active receipt and reservations. The pause
and its operator question commit under the control lock. Allocation recovery,
nonrenewable recovery, conductor/refine refusals and runtime metering share this
path. A valid pending question keeps its identity. Missing or dismissed cards
are repaired when that path is reached again; status reports
`active_paused_without_actionable_question` for existing orphan pauses and
cards blocked by a pending or unknown decision outcome.

The recovery card offers a local stop request, using the existing
`hold` decision effect, and cancellation waits for normal reconciliation.
Claim and resolution commit under one control lock, including the durable
request acknowledgement, so resume cannot interleave with a local stop.
It offers no continuation, funding or GitHub mutation. An issue-body rewrite
refreshes a still-active paused non-watchdog card under its observed control
version and decision identity. The audit retains the prior card and both
identities; no operator answer is inferred. A competing answer, resume or
handoff fences the refresh. Pinned older-generation tasks retain refresh only
under the existing active-receipt decision flag. Active watchdog cards keep their own contract
(#7165). Feature defaults are unchanged. Historical audit attribution and
post-rollout observation remain acceptance work on that issue.

(see: /projects/monolith/factory/orchestration/factory_decisions.py)
(see: /projects/monolith/frontend/src/routes/private/factory/escalations/+page.svelte)

## Controls and uncertainty

`pause_admissions` allows already admitted work to finish. `pause_task` stops
new nodes for that task. `stop` durably fences factory admission and descendants,
including the shared pending-message sweep and transport creation/invoke retries.
The coordinator makes at most two recorded cancellation attempts per active node.
Other operator-owned sessions are outside this control scope.

MCP and `POST /api/swarm/factory/control` use
`factory_controls.request_control` for these supported actions. The caller must
supply a stable `request_key` and the exact control `expected_version` read from
status. The owner records refused stale requests too, so the same request cannot
become effective after state changes. `cancel_receipt` settles one queued
receipt as cancelled through the same ledger and version check, without
changing control state; only queued receipts move, and it takes a `receipt_id`
rather than a `task_id`. `cancel_task` requires a `task_id` and an explicit
`cancellation_reason`. It accepts admitted and uncertain receipts, including
paused tasks, and refuses landing or settled receipts. Its acknowledgement
records the authenticated operator and fences new starts. Ordinary ticks drain
existing attempts through their normal accounting. Cancellation settles with
`operator_cancelled` evidence only after both the start and graph-run ledgers
are quiescent. Unknown invocations keep their reservations. The issue, its
labels and its PR remain unchanged, and intake retains its normal cooldown.
The HTTP route carries neither receipt id nor cancellation reason, so these
cancellation controls are MCP-only. `configure` remains a separate bearer-only
policy operation and is not exposed through MCP. Priority edits, free-form
direction, budget mutation and exact-attempt MCP stop have no supported owner in
this surface and remain unavailable.

A task pause written by the reconciler is eligible for cancellation after two
hours only when all starts already have terminal evidence. An unresolved start
keeps the pause, original cost and capacity hold and records
`reconciler_pause_expiry_held`; elapsed time cannot settle it at zero. Only a
successful reconciler pause audit is eligible. A pause written by an operator
never expires automatically.

A network operation already in flight can remain uncertain after stop. Status
continues to show those reservations and cancellation requests; the stop flag
does not assert that every guest has ceased. Stop is terminal for this first
bounded lane. It cannot be reset by replaying an earlier enable request.

The MCP task row reports stop state conservatively. A running graph node is
`work_still_running`; a reserved or uncertain start without a running node is
`unknown_or_unreachable`; a request with neither condition remains
`cancellation_requested`. Only exact positive stop evidence for every owned
workflow produces `cessation_confirmed`. Cancellation acceptance, lease expiry,
deadline backstops and terminal database state do not count as that proof.

The historical deadline-release implementation is hard-staged off in addition
to `FACTORY_DEADLINE_BACKSTOP_ENABLED=false`. An environment change cannot
activate it in this delivery. Its warning remains useful, but deadline expiry
does not release a start, a task slot or an admission permit. Such an attempt
is released instead by supervised cessation (below), on evidence it produces.

Agent workloads have a twelve-hour runtime backstop. The caller's result wait
and routine drainer observation wait exceed that ceiling. The CLI silence
backstop is eleven hours and fifty-five minutes: a quiet build is not by itself
proof of a stuck task. DAG node limits remain explicit immutable policy;
raising the runtime ceiling does not rewrite admitted attempts or grant retries.
Fresh sessions have a one-day lifetime, and an older session has only the time
remaining before its absolute expiry. Idle parked sessions still expire after
one hour.

Permit supervision settles an hour-old unbound `kg` or `project` permit when
its session is terminal and no durable binding evidence exists. For a bound
non-factory guest parked or banked on a departed node, it requests destruction
with the observed generation, at most twice, and keeps the permit uncertain
until the control plane reports cessation.

The stop path fences admission and requests DBOS workflow cancellation. An
uncertain attempt retains its capacity until exact control-plane evidence proves
that its owned guest ceased. A parked guest on a departed node is first asked to
destroy, then settled only after the later `destroyed` view. Issue #6091 tracks
the control-plane root cause that can otherwise leave this state stranded.

An invoke that ends in the at-most-once 502 ("session invoke failed") is
uncertain, but the control plane has already decided its guest's fate:
`Session.fail_and_stop` durably moves the session to `failed` with terminal
reason `failed` before it replies, then tears the VM down, and `failed` has no
outgoing transition. With `FACTORY_INVOKE_FAILURE_CESSATION_ENABLED`, stop
supervision accepts that view of the exact recorded guest as cessation
evidence when the failing invoke started inside this dispatch, never
completed, carries no drain `interrupted_turn`, and matches any saved stop
identity. It settles the run, start and permit failed at unknown cost in the
usual transaction and the node's ordinary retry budget decides what runs next.
It sends the guest no stop and no destroy. `failed/brick_gone` keeps its own
proof, and a committed result receipt is adopted first as before. On
2026-09-30 a noded container restart closed the node channel under six
in-flight turns; each guest read back exactly this shape within a millisecond
of its 502, and no other proof accepted it, so the attempts held six of ten
delivery lanes until an operator started supervised cessation by hand.

A monolith rollout can cancel an executor after it has bound a fresh guest
and before it sends the invoke POST. The turn is recorded
`invocation_outcome_unknown` (cause `executor_cancelled`) because the
executor does not record how far it got, and the guest sits idle, then
parked, with no invoke stamp. A guest that has never been invoked has no stop
precondition, so stop supervision refused it as `missing_stop_precondition`
and the attempt held its lane until supervised cessation, two hours past the
task deadline. With `FACTORY_NEVER_INVOKED_GUEST_SETTLEMENT_ENABLED`, stop
supervision settles it on two pieces of evidence, both of which must hold:

- The control plane says the exact recorded guest has never started an
  invoke. Its view has `turn_seq` 0 and no `invoke_started_at`,
  `last_invoke_at`, `interrupted_turn`, stop intent, stop completion or stop
  precondition. EmberVM writes the invoke stamp and the `turn_seq` increment
  in one durable append before the invoke worker runs, and never clears
  either.
- The local receipt ledger says no POST was sent for this attempt, and none
  can be sent later (`factory_attempt_never_posted`). With result receipts
  enabled, the transport commits a receipt before every physical invoke POST.
  `prepare_receipt` refuses to mint one once the claim and pending row are
  gone. So if no receipt row exists inside the receipt retention window, no
  executor reached a POST before the fence, and none can after it.

The attempt settles failed at unknown cost in the usual transaction. No stop
or destroy is sent, and the idle guest expires on its own TTL. A minted
receipt, any invoke stamp, a drain marker, a saved stop intent, or receipts
being disabled all leave the attempt on the existing path. On 2026-10-01 the
01:37 rollout cancelled the executor for permit 10690 eleven seconds after
its claim and 1.5 seconds after its guest was created. The guest read back
parked with `turn_seq` 0, and the session had no receipt.

`FACTORY_DRAIN_RELAY_CONTINUATION_ENABLED` carries a brick-drain relay
(#6256) through to an owner, in three places:

- **Node workflow.** `read_factory_dispatch` used to report any second
  dispatch of the node's turn as `unconfirmed`. That ended the workflow
  `uncertain` as soon as the relay claimed its continuation. The flag accepts
  a second or third dispatch when the turn row is the drain or
  brick-preemption interruption that granted it. The workflow then waits for
  the continuation's result under the same attempt and keeps the earliest
  execution deadline it observed. A re-claim without that turn is still
  `unconfirmed`.
- **Retirement.** A continuation that outlives its terminal workflow is still
  retired by `read_interrupted_factory_continuation`. Before, that proof
  refused a priced interrupted turn, but every drain response carries the
  spend of the dispatch it interrupted. The flag accepts a priced turn. The
  conductor still settles it at unknown cost, so the reserved ceiling stays
  charged.
- **Stop supervision.** When the observer is lost before the relay (a monolith
  rollout), the hold is settled as an unknown invocation, no pending row
  remains, and the parked guest has no stop precondition. The flag lets stop
  supervision settle that attempt when all of these hold:
  - the guest is `parked` or `banked`;
  - its `interrupted_turn` names this exact dispatch ID and a transcript path;
  - its invoke started after dispatch and has completed;
  - any saved stop identity matches.

  This settlement is also at unknown cost.

On 2026-10-01:

- Sessions 13510, 13517 and 13518 ended `uncertain` 1 to 6 seconds after
  their 00:44:45 relay claims. They were drained again at 00:48:00 and held
  their lanes behind an unclaimed third grant that only the price check
  refused.
- Session 13635 lost its observer at 01:59. A brick idle drain then parked
  its guest with exactly this marker.

### Attempt-stop ownership

An attempt stop crosses the factory/session boundary through public APIs.
`factory.orchestration.factory_attempt_stop` owns task authority, run/start checks, idempotency
and stop audits. `factory.execution.factory_stop` owns session/permit identity,
the UNKNOWN transition and the executor heartbeat check. The factory calls
`factory.execution.api` with immutable values and its current transaction; the
session domain calls `factory.orchestration.api` to read committed stop authority and exact
cessation evidence. Neither side imports the other's models or private helpers
for this path. The lock order stays factory control, capacity pool, session,
and the UNKNOWN transition commits or rolls back with the factory's stop intent.
Ember still owns physical execution cessation; a cancellation acknowledgment
alone never releases the permit.
(see: /projects/monolith/factory/execution/factory_stop.py)
(see: /projects/monolith/factory/orchestration/factory_attempt_stop.py)

### Passing PRs get a verification handoff

An exhausted implementation can open `implement_handoff_1` and
`review_handoff_1` in one engine-owned graph edit when all six conditions hold:

1. The deviation is `node_failed` on an `implement_` node. Escalations and
   investigation, correction, integration and review failures stay with the planner.
2. The task's version ledger contains no applied `factory_handoff` round.
3. Exactly one open PR names the task's delivery branch, repository and base,
   matches any granted PR number, and has no definite merge conflict. Drafts qualify.
4. `pr-checks` and every non-advisory context succeed at that PR's exact head.
5. Every live direct dependent is an unrun, unarmed review leaf.
6. Both the implementation and reviewer pool heads exist and are policy-allowed.

The edit retires the failed node and those review leaves together. History and
charges remain. The implementation verifies the existing diff without source
commits, repairs only a missing closing line in the PR body, and readies a
qualifying draft. The reviewer supplies independent exact-head evidence. A
changes-requested verdict uses the ordinary correction loop. A handoff consumes
no review round; its separate ledger bound is one per task, even after discard.
Unexpected GitHub data, read errors, or graph and envelope refusals leave the
existing planner path available in the same tick. Opening and skip reasons are audited.

**Why.** Joe chose option 3 on #6296 on 2026-10-01 to remove the person from
this loop without assuming an infrastructure-dead attempt never reached a
model. `attempts_spent` and `NO_MODEL_POST_PHASES` stay unchanged.

### Supervised cessation

Cessation evidence may be produced by supervised termination, not only waited
for. Every other proof above waits for one fingerprinted shape to appear, and
an attempt whose shape never appears held its lane slot and admission permit
indefinitely (#6025, #6288). `factory_cessation.py` closes that gap for an
attempt whose run and start are both `uncertain` and whose node workflow is
terminal:

1. It records a `cessation_intent` naming the attempt's fingerprint
   (`read_stranded_factory_attempt`) and the exact guest it will terminate,
   committed before any external effect.
2. It destroys that recorded guest through the ordinary EmberVM destroy route,
   at most twice, each `cessation_request` committed before its call.
3. It observes the recorded guest, never the live binding, and settles only
   after the control plane has reported it gone across the same sampled window
   as the #6156 absence proof (`MIN_ABSENCE_OBSERVATIONS` readings spanning
   `ABSENCE_CONFIRM_SECONDS`, gaps bounded by `ABSENCE_MAX_GAP_SECONDS`). A
   404/410 and a terminal state (`destroyed`, `evicted`, `expired`, `failed`)
   both count as gone, because the control plane keeps a torn-down guest's
   record until op-log compaction. Any live reading writes
   `cessation_presence` and restarts the window.
4. It settles through `_settle_failed_attempt` in one transaction: run and
   start failed, the permit settled `supervised_cessation` with cessation
   confirmed, pending messages consumed, cost unknown so the reserved ceiling
   stays charged.

The fingerprint excludes the binding fields on purpose. Destroying a guest
clears `ember_session_id`, and a proof keyed on the live binding refused the
attempt it had just stopped (#6288). Settlement accepts only the recorded guest
or a cleared binding, and refuses any other fingerprint change. When a binding
was already cleared before any intent, the guest is recovered only from the
monolith's own receipts and earlier supervisor audits, and exactly one
candidate is required.

An attempt with no binding evidence at all has no guest to destroy. It settles
at zero only when the exact lost-before-guest proof holds, and otherwise at
the reserved ceiling as `never_bound_released`.

This keeps the rule that elapsed time is not cessation evidence. The deadline
decides only which attempts are worth acting on; the release still waits for
the control plane to report the recorded guest gone. The automatic path runs
more than two hours past the task deadline, paused or not, behind
`FACTORY_ACTIVE_CESSATION_ENABLED` (default off). A past-deadline task left with
no unresolved start then finishes `escalated` with a card, so a person owns the
question. `factory_cessation.request_cessation` runs the same steps for one
exact attempt on an operator's reviewed fingerprint (`read_cessation`),
whatever the flag says, and never cancels the receipt.

## Validation

New tests have explicit targets in `projects/monolith/BUILD`. File-backed
hermetic tests cover concurrent receipt deduplication and admission, immutable
policy and pins, rollback across both reservation ledgers, task and node limits,
latest-review evidence, and the complete issue/planner/work/review/PR sequence.
Transport tests cover stop during capacity retries and independent concurrent
contexts. Linux orchestrator CI and a real bounded operating trial remain the
delivery gates; local tests are advisory.


### Independent conductor watchdog

The factory owns task decisions and durable recovery state. Kubernetes owns
process recovery through the existing backend `/healthz` liveness probe, so
watchdog execution does not depend on the conductor, DBOS, a model grant, or
the agent session queue. The factory module supplies a process-local liveness
check through the framework's `register_liveness` hook. Public profiles do not
run private liveness checks.

The watchdog arms when the elected leader starts the conductor. A stopped
conductor task fails immediately; a running loop fails after 600 seconds
without a completed reconciliation pass. The existing kubelet probe then
requires six consecutive failures, checked every ten seconds, before replacing
the backend container. Healthy followers and disabled factories stay healthy.
Leadership resignation disarms the watchdog before runtime shutdown.

A completed pass counts as progress even when it returns an error, because
the reconciler can retry a dependency outage without a process restart. Agent
turn duration, quota waits, and paused admissions do not age this signal while
reconciliation continues. The probe performs no I/O and does not cancel work,
re-admit issues, settle uncertain outcomes, raise budgets, or make model calls.
After replacement, normal workflow recovery and reconciliation use the existing
receipt, attempt, session, and accounting identities. HTTP responsiveness alone
no longer hides a conductor that has stopped making progress.

## Funding

Task funding is implemented in `factory_funding.py`. `request` records one
judge node, `settle` verifies its authority, and `decide` resolves a validated
artifact to `continue`, `steer` or `stop`. A steer may require an operator
instead of granting work. The caller commits the grant or stop under both the
control and task locks. The judge never writes policy directly.

`FACTORY_CONDUCTOR_FUNDING_ENABLED` enables new funding reviews when work turns,
review rounds or a lease run out. Existing enrolled reviews can finish after
creation is disabled. The selected policy judge has one exact dispatch, a
five-minute admission deadline and a recorded $1 reservation. Only that model
may settle the review while the effective policy still allows it. The issue,
graph revision, work evidence and policy digest must still match. A failed or
stale review waits five minutes before retry; six consecutive refusals end the
task with its evidence retained. Queueing beyond the admission deadline does
not invalidate a completed review whose authority remains current.

A continue or steer grants at most 20 work turns and a review horizon of at most
30 minutes. The horizon fences new work; reserved workers keep their invocation
deadlines. The original policy stays immutable and `funding_granted` supplies an
effective overlay. With merit judging enabled, a recorded turn deficit raises
the requested tranche and the pinned turn ceiling limits it. A deficit that
cannot fit becomes an operator decision. No amount of oversight or accounting
correction constitutes new work evidence for another automatic grant.
`factory_funding.funding_blocker` enforces this check and preserves its pause
and capacity-release revocation.

Objective accounting sums execution and funding across task generations and
advisory/delivery passes. Immutable admission audits retain ownership across
readmission. Reservations and unknown outcomes stay charged. Observed costs do
not raise or shrink the approved dollar figure. The existing explicit budget
amendment boundary remains: a judge proposing a larger budget pauses the receipt
for operator approval, without granting the tranche. The funding decision
function separates that proposal from permission to execute.

An unresolved funding escalation or budget proposal cannot request a second
judge. A successful operator resume or exact policy amendment records
`funding_progress_rearmed` at the current settled-turn count. Both the progress
trigger and funding reconciler honour that answer. The next progress review
waits a full step; reservations, charges and legacy watchdog holds are retained.

Exact-artifact continuation also uses `factory_funding.decide`. Its explicit
operator authority retains the same PR, independent reviewed head, subscription
admission and landing hold. Renewal still refuses repeated review failures or
new failed execution. Historical `progress_continuation_*` audit rows remain
readable and the lease projection retains its existing vocabulary.

`FACTORY_MERIT_JUDGE_ENABLED` adds the criteria and server-read evidence in
`factory_judge.py`: progress, proximity to landing and issue value. It also
allows a judge's explicit doubt to steer to an operator card. That artifact must
carry an assessment. Without merit authority, `escalate` is refused. The pinned
turn ceiling is the original turn allowance plus
`factoryMeritJudgeMaxExtensionMultiple` times it (at least two extra turns),
never a multiple of the last grant. A stop retains the issue, PR and findings.
Independent exact-head delivery review and required CI remain mandatory.

Reservation supervision owns the currently running invocation.
`FACTORY_RESERVATION_REVIEW_ENABLED` gives it a 30-minute lease, with a bounded
review starting five minutes before expiry. One durable reviewer uses reserved
interactive headroom. Heartbeats never renew the work lease. Approval requires
new progress and a known running guest or an owned pre-dispatch reservation.
Steering and stopping use exact-attempt cessation; guidance reaches the next
plan only after confirmed cessation. Unknown results, queued user input and
charges survive. A failed reviewer's historical guest binding releases its
concurrency hold only on exact cessation proof, with no newer work, aliases,
cleanup ownership or observer fences. Health reports blocked and overdue
reviews separately from process liveness. `factory_funding.decide` resolves the
reviewed reservation verdict after current evidence is read under the pool lock.
The funding module owns the model-invocation step. Identity checks and effects
remain in `reservation_reviews.py` and the execution lease layer. The verdict's
canonical audit commits with the lease state, including exact permit and review
identity; pre-dispatch factory work retains its task attribution.

The no-progress adapter counts settled work and planner turns against the
configured threshold (default eight). New commits or PRs rearm it. A crossing
without a delivery requests the same durable funding review as an exhausted
allocation. There is no second classifier or new watchdog card. Pending reviews
are shared, failed reviews keep their five-minute retry wait and refusal limit,
and only a successful settlement advances the progress checkpoint. Unresolved
attempts wait for their existing owner. Legacy dollar-threshold audits rearm from
the current turn count. Existing watchdog cards still enact their operator's
Resume, Stop or Rescope decision before considering fresh delivery evidence.

The former progress shadow no longer classifies evidence or writes
`progress_shadow_assessed` rows. Its accounting collector remains because the
runtime meter consumes it, including lost start associations and historical
watchdog costs. Old shadow rows stay readable in task detail. The retired
`FACTORY_PROGRESS_SHADOW_ENABLED` setting has no execution or audit effect.
The runtime meter still validates monotonic native usage and exact ownership;
its opt-in usage/backstop stops remain unchanged. The dormant reservation
dollar-ceiling stop path and its Helm setting are removed. Meter admission,
ongoing backstop and stale-usage outcomes now pass through the shared funding
decision. Existing opt-in session/objective backstops, uncertainty checks and
exact-attempt stop authorization are preserved. Accounting coverage must never
be represented as a spending guarantee.

There are two retained continuation contracts. The legacy
`FACTORY_AUTONOMOUS_CONTINUATION_ENABLED` path grants one final, named correction
and independent review pair at a work-turn limit. It cannot fund other nodes or
renew itself. Failure retains the PR and findings. Funding supersedes this pair
for enrolled tasks. Separately, an operator's `progress_continuation`
preview/apply authorizes one paused recovered task and its native-reviewed PR
head for a two-hour lease. It binds recovery, graph, policy, accounting, funding
and direction without changing cash fields or clearing landing/security holds.
Renewal requires a distinct independently reviewed head, green CI, fresh
subscription evidence and quiescence. Uncertain work, repeated failures, a pause
or changed authority refuses it. Stop-only cards acquire no continuation
permission. Automatic renewal uses the same funding decision and review-wait checks.

A saved successful final conductor finish can settle after its lease expires
when graph/run evidence is unchanged, all starts are accounted, and fresh owned
PR CI and independent exact-head review pass. It creates no new execution or
funding authority. Paused, cancelled, human-owned, uncertain and changed tasks
remain fenced. A typed `landing_hold` can restrict a receipt without extending
its deadline or altering its pause. Ordinary amendments of recovered tasks bind
the existing recovery and funding audits and retain their absolute deadlines;
changing the timeout is refused.

Funding writes retain the `funding_review_requested`, `funding_granted` and
`funding_review_settled` audit family. Existing report and DORA readers, stop
ownership and historical rows are preserved. Progress observations now use
`funding_progress_cleared`, successful funding settlements carry progress
checkpoints, and old-card refusal writes use `funding_legacy_hold_refused`.
Continuation and meter records now have canonical `funding_*` names; reservation
verdicts write `funding_reservation_decided`. Historical rows remain readable.
Marked legacy projections commit atomically with canonical events solely for old
binaries during rolling upgrades and rollback. Current readers exclude those
projections; a later unmarked legacy write is still honored. This retains one
current decision authority without double-counting usage or losing old-reader
lease/trip enforcement. Reverting the PR requires no schema rewrite. Required
Linux/PostgreSQL tests and exact-head independent review remain delivery gates;
source/test reduction percentages are measurements, not numeric gates.


### Native registration and caller discovery

Bounded factory status exposes hashes and argument names for the existing refill
and receipt-amendment controls, without claiming it can inspect or refresh a
client catalogue. Registration and description validation run against the real
module registry. Authenticated local integration tests exercise restriction and
separately authorized continuation through that same MCP schema, preserving
expired paused deadlines and uncertain execution. No second control route or
access change is introduced to work around stale discovery.
(see: /projects/monolith/factory/MCP.md)

### Work item pointer comments

`FACTORY_WORK_ITEM_POINTER_ENABLED` and `FACTORY_WORK_ITEM_BASE_URL` (default
`https://private.jomcgi.dev`) enable GitHub comments that link each work item
to its durable factory record. One comment per GitHub-sourced work item, created
once and edited on mint, transition and authority changes, never read back. The
comment includes a `<!-- work-item:ID -->` marker, the work item state, and a
link to the work item detail page. Pointer comments exist only for display and
reference; labels, changes and comments on the GitHub issue are not mirrored
back.

The sync runs at most 20 items per tick. A failed GitHub write triggers
exponential backoff: wait 2^n minutes (capped at 1440 minutes / 24 hours) after
the nth failure, then retry. A successful write clears the backoff counter. The
sync excludes closed work items and skips work items lacking a GitHub issue
number.

The `/factory/work-items/{id}` page ships work item details, edges, and recent
events. All edges show their source (manual, github_body, or decision) for
transparency about how each relationship was created.



### Conductor-owned reversible gates

Refine, investigate and planner artifacts carry a typed `gate` when a missing
parameter, unavailable live check or existing delivery target would otherwise
raise a decision card. A parameter classification of `reversible` includes its
proposed `value` and `reason`. The builder chooses it and comments
`Decided by the conductor: <value>, because <reason>; reversible` once.
`spending`, `prod_deletion` and `external_account` keep the human decision path.
A case-insensitive heuristic backstop also escalates restricted terms in a
gate's proposed value, even when the model labels the gate reversible.
Every gate requires an `effects` object with exactly four booleans:
`spends_money`, `deletes_data`, `touches_external_account`, and `uses_credentials`.
Declare each true when choosing the value or acting on the gate would have that
effect, including using or changing credentials. Any true effect keeps the human
decision path for every gate kind. Resolution and refinement share this check;
`reason` is free prose and may explain safety. Recording a staged live-validation
scope and its outstanding checklist does not authorize performing those checks.
A documented default does not authorize bucket creation, deletion or credentials.
New and in-flight artifacts with missing or malformed effects fail validation
and cannot resolve. Stored conductor decisions remain readable without effects.
Unclassified legacy questions retain their existing escalation path.

For `live_validation`, the conductor records a default-off or staged repository
`scope` and appends `live_checks` as unchecked lines on the issue. The PR body
states `Conductor rescope:` and retains references without closing keywords.
The delivery gate refuses a PR that closes pending operational acceptance, and
landing records `repository_delivery_complete` after the managed rollout is
verified without closing the issue. Required Linux CI and independent exact-head approval still apply.
Refine decisions carry into delivery admission; the receipt preserves decisions
across restarts and every planner and worker receives the current scope.

#### Handing off live checks

Normal plan edits and completed work artifacts persist typed operational gates
before further work and before publishing their GitHub rescope. Gate and graph
decisions have separate identities, so an edit interrupted after gate persistence
can still retry. Re-admission retains the checklist in receipt direction.
Landing reloads it before closure and independently checks that the merged PR
confirms completion of this issue. A reference-only or explicitly rescoped PR
leaves the issue open even after rollout verification.

`FACTORY_OPERATIONAL_HANDOFF_ENABLED` (`swarm.factoryOperationalHandoffEnabled`,
default false) makes "the repository work is delivered, only live checks
remain" an autonomous outcome rather than a page. Enabling a production flag
stays a person's decision; the hand-off records that ask, it does not page it.

- **Planner.** A delivery planner that finds its scope already merged returns
  a `repository_delivered` gate (`delivered_prs`, `live_checks`, optional
  `tracked_in`) with its pause. The server verifies each PR is merged in this
  repository and references the issue (`#N`, `owner/repo#N` or its URL). It
  then opens one child issue carrying the checks and `needs-human`, never
  `agent-ready` (fenced by `operational_handoff_child_created`), or uses the
  open `tracked_in` issue the rescope already names; takes `agent-ready` off
  the original, closing it as `completed` only in the `tracked_in` case; posts
  one marker-fenced comment; audits `repository_scope_delivered`; and settles
  the task `cancelled` with state `repository_delivered`. No decision card and
  no Discord message are raised: the daily digest lists the audit. A PR that
  does not verify audits `operational_handoff_refused` and keeps the card. A
  refine gate never takes this path, because a resolved refine gate applies
  `agent-ready`.
- **Intake.** An issue whose receipts carry `repository_scope_delivered` is
  excluded as `repository_delivered`, like `delivered`. So is one whose earlier
  task recorded a `live_validation` gate and whose factory PR (from
  `factory_pr_settlement_complete` or the receipt's delivery target) has since
  merged and references the issue; the proof is recorded as
  `repository_scope_delivered` so later sweeps read no PR. Those reads are
  bounded at five per sweep; a candidate left unproven by the budget or a
  failed read is excluded as `delivery_check_deferred` for that sweep rather
  than admitted on a guess.
- **Split children.** A `split` child marked `operational: true`, or whose
  title names a live or operational step (`OPERATIONAL_TITLE`), opens with
  `needs-human`, so intake does not brief it.

**Why.** On 2026-09-30 the generation bump from 13 to 14 re-admitted #6288,
#5803, #5787, #5758, #5757, #5699 and #4321, whose earlier receipts had failed
or escalated after their `live_validation` rescopes. Each conductor correctly
found the repository work merged and only live checks left, then paged.
Deciding those cards with `split` opened children without an exclusion label,
and intake admitted #6559 as a refine task (receipt 724) seconds later.

Before its first node, a delivery task discovers open PRs closing its issue,
including operator re-posts. It adopts the oldest matching PR (preferring an
already granted match), records the branch and PR on the receipt, and directs
rebase onto main, repair and fresh independent review. Discovery failure or
truncation waits without starting a competing branch. A running branch owner
is named in an escalation; no second writer starts without that decision.
Only existing PR heads in this repository under `factory/` may be adopted.
Heads outside `factory/` escalate with the PR author's login. The base branch
and another repository's head are never adopted; fork and deleted-fork PRs
are excluded from discovery candidates.

Discord uses one durable human-needed fence per task and notification kind:
refine, escalation, intervention, deadline and landing. Supervision combines the attempts
known at the time into one summary. Before notifying, it folds observations and
subsequent confirmed stop settlements, authenticated drain settlements, and
reconciled terminal start outcomes in audit order for each exact workflow.
Settled attempts retain their history but do not consume the notification fence.
Uncertain outcomes and other attempts do not clear an outstanding warning.
Later observations remain in the audit
instead of sending another message. Automatic stall recovery is audit-only.
A failed notification attempt is audited without consuming the fence, so it
can be retried, and does not block task settlement.

#### Paging and the daily digest

`FACTORY_NOTIFY_DIGEST_ENABLED` (`swarm.factoryNotifyDigestEnabled`, default
false) decides per notification kind whether the operator is paged
(`factory_notify_policy.pages`). Pages: `escalation` (a decision card: a
planner pause, which the charter reserves for human authority; a funding
question the merit judge escalated or a hard cap blocked; a branch another
owner holds), `watchdog:*` (retained historical cards), and
`landing` (a pull request left to merge). `intervention` and `deadline` page
only while nothing will release the slot on its own; with active cessation or
the deadline backstop on they digest. `refine` needs-human briefs digest.

A digested notice is audited as `task_needs_person_digested` under the same
per-task, per-kind fence and sends nothing. Once a day the reconciler sends
one `info` message (`factory_digest_sent`, with its audit watermark) listing,
since the last digest: the merit judge's funding grants with requested and
granted figures, historical watchdog assessments that let a task continue, issues whose
live checks were handed off, and the digested notices, with the escalations
link. A failed send is retried an hour later. The first digest after enabling
covers the previous day rather than all history. Decision cards are not
changed: every card stays on `/factory/escalations` whether it paged or not.

**Why.** Joe was paged for notices the factory then handled. Every one of the
24 intervention pages in the week to 2026-10-01 was settled automatically by
active cessation, stop supervision or a drain relay, and refine briefs ask a
minute's scoping question on advisory work. Authority decisions still page;
the rest, and what the factory approved on its own, arrive once a day.


### Restrict receipt model choices

`factory_amend_policy` accepts a separate `{"remove_models": ["spark"]}` overlay
to remove unused models from one paused, quiescent receipt. Preview shows the
allowlist and pool changes; apply requires its digest and a request key. It
filters names in place without reordering the survivors or changing scalar
defaults. A model used by a current graph pin, task choice, scalar default or
pending funding review cannot be removed. Explicit pools must remain nonempty.
No other policy or hold field can accompany this overlay.

The restriction is stored on that receipt and applied after funding overlays.
The original policy snapshot, retired graph nodes and historical attempts remain
unchanged. Holds, accounting, generation, recovery grants, funding reviews and
deadlines are preserved, including expired deadlines. Apply leaves the task
paused and creates no funding, execution or lease authority. Its audit records
the reviewed preview and supports replay after a lost response.

The restriction survives operator chat and decision requeue of the same receipt.
Fresh admission refuses a configured scalar or empty pool that conflicts with it.
Native execution checks the actual queued model before delivery and physical
dispatch, including a new message on a historical session. Historical pins and
results remain unchanged.


### Settled learning execution

`factory_reconcile_learning_execution` lets a standing operator preview and
acknowledge one exact `factory-retro-daily` session/turn/permit after a fresh,
positive control-plane cessation read and locked checks of the terminal worker,
job, dispatch, receipts and pending work. Missing remote records are not proof.
The digest-bound, idempotent audit changes neither execution history nor the job.
Only the hourly cadence guard consumes it, and changed evidence invalidates it.
Refill enablement itself does not inspect historical learning sessions unless
the request also changes cadence. Recovery does not enable either operation.

A typed missing guest projection may select a fresh, authenticated EmberVM
historical-cessation verifier. Its narrow original-generation parking and idle
eviction chain must survive journal compaction with complete creation coverage;
all current lineage holders, successor creations, dispatch-capable restore work and unresolved
residency claims refuse proof. A surviving terminal holder also refuses because
it could still authorize a workspace restore. Monolith checks the authority's
thirty-second freshness separately, binds stable proof facts into preview/apply,
and rejects other current or prior local lineage owners under its existing locks.
The audit records the authority read time separately from the stable digest.
Only this cadence acknowledgement consumes the proof; historical unknown results,
accounting, task holds and normal session-send fences remain intact.
The proof is scoped to EmberVM's one-replica `Recreate` authority. A dead
manager's orphan Restore/Prime operation cannot register or dispatch through its
retired owner; absence of orphan resource work is not asserted.

### Settled quota-probe reconciliation

`factory_reconcile_quota_probe` provides standing operators an exact preview/apply
for a failed Claude probe whose permit supervision already proved no guest delivery.
It binds the original probe-start audit, session, sole turn and permit, dispatch
identity, settlement evidence and control version. Any binding or prior binding,
workflow, cleanup, pending work, receipt, ambiguous attempt or changed proof refuses.
Apply requires the preview digest and a stable request key. It appends a separate
resolution audit without rewriting the unknown turn, permit or accounting. The
normal probe selector revalidates this exact evidence before treating that historical
unknown marker as resolved. New evidence blocks again. No generic session retry,
quota observation or factory-task continuation is performed by reconciliation.
Only the ordinary rate-limited probe can obtain real response-header quota evidence.

## Factory evolution follows delivery authority

The Conductor pursues continuous self-improvement and useful delivery velocity
toward Joe's goals. Factory-owned budgets and workflow policies may evolve
through their owning interfaces; dollars remain accounting. Root `AGENTS.md`
defines standing delivery authority and
the [evolution contract](../../../../docs/agents/evolution.md) defines how
factory improvements are evaluated and retained. Routine design, security
repairs and decision-record updates within accepted scope can proceed
autonomously. Conflicting goals, human-owned constraint exceptions, unapproved
destructive work and explicit human-owned decisions or holds need resolution
by their owner. Existing receipt limits and GitOps boundaries still apply.
Factory prompts own the action sequence. Existing goals, issues, receipts and
audit events retain state.

The existing scout collects and independently reviews goal-linked findings
before ordinary issue refinement. This adds no scheduler or policy enablement
and changes neither pinned task limits nor explicit holds.
(see: /projects/monolith/factory/orchestration/factory_improvement_scout.py)
(see: /projects/monolith/factory/orchestration/factory_improvements.py)
