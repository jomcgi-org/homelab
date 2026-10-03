# Drain queue (Luna lane)

The job kind (`qwen-drain`), PR label (`qwen-agent-for-review`), git identity
(`qwen-drainer`) and branch prefix (`qwen/`) keep the name of the model the lane
was built on. The default worker is Luna: `DRAIN_MODEL = "luna"` in
`projects/monolith/factory/orchestration/drainer.py`, a Codex-family model on
claude-runtime. A `qwen-drain` job may name another model from `DRAIN_MODELS`
(`luna`, `sol`) in `payload.model`; KG jobs always run on Luna.

The drainer runs `claude_agent.routine_jobs` rows of kind `qwen-drain` as one
fresh Luna session each, strictly serially, claimed by a `*/15` CronWorkflow
tick. The lane spends subscription quota rather than per-token billing, so it
exists to turn idle capacity into audits, reports, and small PRs. The same
drainer claims `kg-drain` jobs for knowledge extraction, and `docfix:` jobs are
one-shot `qwen-drain` tasks for human-reviewed documentation PRs.

The default-off KG audit uses that same `kg-drain` admission, quota floors and
daily job cap. Its recurring `kg-audit` payload is
`{"mode": "audit", "stream": "scheduled"}`. One-shot expansion jobs named
`kg-audit-x:<root_run_id>:<depth>` carry `mode: audit`, `stream: expansion`,
`root_run_id`, `depth`, `note_ids` and `parent_finding_ids`. The root ledger
reserves at most eight neighbours across two depths, including pending jobs.
These jobs validate pinned repository-scoped samples; ordinary self-disputes
go to separate resolver sessions. Process-issue filing runs after scheduled
apply in its own best-effort step. The audit neither gates nor reroutes work.
`knowledge.audit.enabled` disables the whole loop, and clarity repairs and
issue filing have separate default-off switches.

## Queueing work

Register with the MCP tool `monolith-agent-register-routine-job`:

- `kind: "qwen-drain"`, `payload: {"prompt": ...}` (optional `repo`, `branch`,
  `reasoning`, `model`, `digest`).
- `model` picks the worker: `luna` (default) or `sol`. Any other value fails the
  job before a session starts. The claim reservation, the session and the
  quota check all use the job's model: the lane-wide gate judges Luna, and a
  Sol job is also deferred when Sol's own provider is walled (Sol and Luna
  share the Codex grants, so in practice the two readings agree).
- `digest` appends a server-built evidence block to the prompt at claim time.
  The only value is `factory-retro` (see below).
- One-shot jobs: `interval_secs` null, `next_run_at` now. On completion
  `next_run_at` goes NULL and the job leaves the queue; `trigger-routine-job`
  re-arms it.
- Recurring jobs: set `interval_secs`. To edit a prompt, `deregister-routine-job`
  then re-register with the original schedule.
- `next_run_at` is a priority field: `claim_job` orders by it ascending, so a
  past timestamp jumps the queue.
- Names are unique forever (a completed one-shot keeps its row), so a re-run
  needs a fresh name. Use a dated batch prefix such as
  `qd0828-<template>-<path-slug>`.

For more than about ten jobs, call the same function in the pod from a JSON
file on stdin, which keeps the real validation:

    kubectl exec -i -n monolith deploy/monolith -c backend -- \
      /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 -c '
    import sys, json, datetime
    sys.path.insert(0, "/projects/monolith/main.runfiles/_main/projects/monolith")
    from agent import routine_jobs
    for j in json.load(sys.stdin):
        routine_jobs.register_job(name=j["name"], kind="qwen-drain",
            interval_secs=None, payload={"prompt": j["prompt"]},
            next_run_at=datetime.datetime.fromisoformat(j["next_run_at"]),
            created_by="batch")
    ' < batch.json

## Sizing and depth

The timeouts are backstops, not a size limit: the drainer's
`turnTimeoutSeconds` is 43800 and the EmberVM invoke backstop is 43200s. Size
jobs by search space: per-file and per-directory questions converge, repo-wide
sweeps do not. Split "audit all runbooks" into one job per runbook.

`agents.drainer.reasoning` and `payload.reasoning` become the invoke's
`thinking` field, which only pi sessions read. Codex effort is pinned per model
in the EmberVM shim's `CODEX_MODELS` table (`luna`: `medium`, `sol`: `high`),
so choosing `payload.model` also chooses the depth; change that table to change
it. There is no per-job effort. Nothing ends a looping Luna turn short of the timeouts, so
fix a looping job's search space or prompt.

## Writing job prompts

- One bounded question with an explicit output contract ("one line per
  finding as 'file: problem'").
- Exact numbered steps only for the PR recipe, where one sequence is the safe
  one. For audits, state the question and the contract and let the model plan.
- A verification clause: "verify each claim with an actual grep or ls before
  reporting it", so every finding carries evidence a reviewer can check.
- A size cap ("keep the whole answer under 1800 characters"): summaries land
  in `last_summary` and Discord.
- Report-only jobs open with "Report-only task, do not modify files."
- "Never use em dashes" for anything that lands in commits or PRs.

Bound the search space before asking for a fix. One named file, then fix it:
allowed, because the file bounds the discovery and the PR carries its evidence.
A directory or "somewhere in the repo", then fix it: report only, and let the
dispatcher pick what is worth fixing. A fix that needs judgement (superseded
architecture wording, whether a doc is aspirational, anything over one line) is
reported, not edited.

Good work for the lane: per-file staleness audits, path-citation checks, TODO
inventories, index-vs-directory drift, single-defect spec'd PR fixes, daily
digests. Poor work: anything needing bazel or the test suite (not in the
guest), repo-wide sweeps, judgement calls on prose, multi-file refactors, and
anything where a wrong answer is expensive to detect.

For a spec'd job: check the anchor is unique (`grep -c`) or state the expected
diffstat, include a `git diff --stat` self-check that reverts and replies
`EDIT FAILED` on anything unexpected, and confirm the replacement value at
source yourself before putting it in the spec. A reply saying your premise is
wrong (a path that does not exist) is a spec bug, not a job failure.

## The PR lane

Drain sessions open PRs with no extra plumbing: the egress sidecar injects the
GitHub token (Basic for git push, Bearer for `api.github.com`), and the guest
image ships git, gh, curl and jq. `knowledge/docfix.py` uses the same identity
and label. The recipe:

1. Find the checkout (`ls /session /session/*`, then cd to the dir with
   `.git`).
2. Make the exact edit (spell it out: file, line, old text, new text).
3. `git config user.name "qwen-drainer"` and a noreply email.
4. Branch `qwen/<job-name>`, Conventional Commit.
5. `git remote set-url --push origin https://github.com/jomcgi-org/homelab.git`
   (the clone origin is the node-local mirror, which cannot receive pushes).
6. `export GH_TOKEN=placeholder` (gh refuses to run without one; the sidecar
   replaces it).
7. `gh pr create` with title prefixed `[qwen]`, label `qwen-agent-for-review`,
   the job name in the body. Never enable auto-merge.
8. "Your final answer must be only the PR URL."

A doc PR touches only the doc: the docs manifests are genrule outputs.

### The audit-and-fix template

One file in, either `CLEAN` or a PR out. Prefer it to report-only for code and
doc changes: a report costs a dispatcher round-trip to become a fix.

    Report first, then fix only what you can prove. Never use em dashes.
    Find the checkout: ls /session /session/* then cd to the dir with .git.

    Audit {target} for drift against the current repo state.
    1. Read {target} in full.
    2. For every repo path it cites, check existence with ls or git ls-files.
       A path the text calls retired or historical is NOT a finding. A URL
       route or a protocol method name is NOT a repo path.
    3. For claims citing a file plus a checkable detail (a number, a default,
       a flag, a filename), open the cited file and check it.

    If you found nothing, your entire final answer must be exactly CLEAN.
    Stop there. Do not create a branch.

    Otherwise fix ONLY findings that are a single-token substitution you have
    verified on BOTH sides: you have seen the wrong value in {target} and the
    right value in the source file. Anything needing a judgement call, a
    rewrite, or more than one line: leave the file alone and report it as a
    line of text instead.

    For each fix, record the evidence: the command you ran and its output for
    the doc side and for the source side.

    Then: git config user.name "qwen-drainer" and a noreply email; branch
    qwen/{job-name}; commit with a Conventional Commit; git remote set-url
    --push origin https://github.com/jomcgi-org/homelab.git; push; export
    GH_TOKEN=placeholder; gh pr create with title prefixed [qwen], label
    qwen-agent-for-review, and a body containing the evidence block.

    Verify with git diff --stat before pushing. It must show ONLY {target}.
    If it shows any other file, run git checkout -- . and reply EDIT FAILED.

    Final answer: the PR URL, or CLEAN, or a list of reported-not-fixed lines.

The evidence block makes review cheap: the reviewer checks whether the two
quoted sides actually disagree instead of re-deriving the finding.

### Review policy

Review keys on diff class, not author.

- **Spec'd job** (the dispatcher wrote the exact edit): check the diff matches
  the spec; PR CI gates it. No separate review pass.
- **Audit-and-fix job** (the worker found the drift): one review, which judges
  both the claim and the edit. Read the evidence block first; if its two sides
  do not disagree, close the PR.

Lane PRs are human-merged, except docs-only `docs:` PRs, which a
`docfix-review` job verifies against main and queues when `docFixAutoMerge` is
on. The protected set (`knowledge/docfix.py`, `DOCFIX_PROTECTED_PATH_GLOBS`) is
always human-merged. Cap a batch at what you are willing to review in one
sitting.

## The factory retro job

`factory-retro-daily` is a recurring `qwen-drain` job on Sol with
`payload.digest: "factory-retro"`. At claim time the drainer calls
`factory/orchestration/retro.py`, which reads the last 72 hours of factory
execution (node runs, planner refusals, infra deaths and repair re-plans,
failing turn results, setup tool calls, review verdicts, list-price cost and
cost per landed line, escalations) plus the open `factory:` / `embervm:`
issues and every issue or comment carrying the `<!-- factory-retro -->`
marker, and appends a bounded digest (under 48 KB) to the job's prompt. The
guest has no database access and the public task pages only cover the board's
live and most recent tasks, so the digest is built server side; its cites are
public links where the page exists, else `#<issue> <node>/<attempt>`.

The window is 72 hours although the job runs daily, because most patterns
need several days to reach signal. The marker list is how a daily run
dedupes: it comments on an existing issue rather than refiling. The guest
files issues with `gh` through the egress-injected token (verified: create,
comment and close work from a claude-runtime guest). To change the prompt,
deregister and re-register with the same payload keys.

## Inspecting outcomes

1. **Open the drain console first**: `/private/agents/drain` in the UI, or
   `GET /api/agents/drain/console` in-pod. It classifies lane state from step
   checkpoints (running under 120s of silence, quiet under 600s, wedged beyond
   that, plus a stranded state for ENQUEUED rows on a stale
   `application_version`), shows each job's tool-call fingerprint, and offers
   cancel and requeue. Read its `state`, not `age_seconds`, which is the cycle
   age: a cycle can sit in `start_agent_session` for up to 19 minutes while
   `create_session` walks the capacity backoff ladder.
2. `monolith-agent-list-routine-jobs` with `kind: "qwen-drain"`: `last_status`
   and `last_summary` are the per-job verdicts.
3. `gh pr list --label qwen-agent-for-review` for the PR backlog.
4. Classify each failure against the table below, harness first: guest and
   deploy failures dominate. Add a new failure mode here in the PR that fixes
   its first occurrence.

Do not triage by regex. Jobs wrap `CLEAN` in prose ("Verdict: NOT CLEAN"), so
`last_summary LIKE '%CLEAN%'` misclassifies both ways. Read the summaries, or
match a finding shape such as `'\.md:[0-9]+: doc says'`.

Two false-positive classes look like real findings: references to
`jomcgi/homelab` (GitHub redirects it after the org move, so they work), and
comparisons the template got wrong, such as a caller-facing MCP tool name
(`monolith-monolith-agent-trigger-job`, FastMCP's double prefix) against its
Python function name. Verify a sample of each template rather than every
finding.

### Failure taxonomy

| Symptom in `last_summary` | Cause | Fix |
|---|---|---|
| `502 :invoke_timeout` after ~12 hours | the 43200s EmberVM invoke backstop | a non-converging job: check the tool-call count in `usage_json`, then narrow its search space |
| `502 {:session_down, ...}`, `503 workspace does not exist`, `All connection attempts failed`, `Server disconnected` | a bad guest; the job was fine | `trigger-routine-job`; it usually passes on a fresh guest. These are terminal, so a one-shot stays dead until requeued by hand |
| jobs due but nothing claimed for hours, ticks firing | a wedged drain cycle holding the concurrency-1 slot | open the drain console. A pod roll does not clear it (DBOS recovery re-enqueues into a PENDING row). The reaper cancels a cycle with no step checkpoint for 1800s; to clear one sooner, `POST /api/swarm/runs/{workflow_id}/cancel` in-pod |
| `next_run_at` NULL, job gone from the due list | the one-shot completed | `trigger-routine-job` to re-run |

### Harness knobs

`agents.drainer.*` in `projects/monolith/chart/values.yaml`: `enabled` (the
kill switch, flipped in deploy values), `maxJobsPerCycle`, `turnTimeoutSeconds`
(43800), `reasoning` (pi sessions only), `stallThresholdSeconds` (advisory
only), `jobKinds` (a list; `[]` pauses all claims), `repo`, `branch`. Session
limits for Luna live in `claudeRuntimeWorkload` in
`projects/embervm/deploy/values.yaml`.

Read knobs from the deployment, not `agent/config.py`: the code default for
`maxJobsPerCycle` is 3 and the chart sets 15.

    kubectl get deploy monolith -n monolith -o jsonpath='{range .spec.template.spec.containers[?(@.name=="backend")].env[*]}{.name}={.value}{"\n"}{end}' | grep DRAINER

A cycle claims up to `maxJobsPerCycle` jobs, runs them serially, and chains
into a successor when it hits that bound with at least one success, so a deep
backlog drains continuously. Over-filling is harmless: unclaimed jobs wait.

## Factory optimizer evidence

The existing `factory-retro-daily` job follows improvement program #6781.
Direct implementation of #6782 through #6786 is reserved to the operator's
manual PR series. Preserve ownership; do not restore agent-ready while a
manual implementation or observation is unresolved.

A selected experiment is recorded in a comment on #6781 with
`<!-- factory-optimizer:experiment:v1 -->` followed by one fenced JSON object:

```json
{
  "issue": 6782,
  "pr": 6788,
  "app": "monolith",
  "expected_revision": "<published chart version>",
  "writeback_commit": "<40-character writeback commit SHA>",
  "metric": "refine_stale_pause",
  "minimum_attempts": 26
}
```

Use actual writeback identity after publication. The example traffic criterion
comes from the baseline's 26 refine admissions, not a significance threshold.
Document the chosen minimum and hypothesis alongside the marker. Other supported
metrics are `dispatch_failure`, `pre_model_failure` and
`funding_execution_failure`. This bounded reader holds as UNKNOWN if the program
has 100 or more comments; consolidate to a fresh explicitly reviewed reader
before that limit rather than guessing which historical selection is current.

The server checks that the writeback contains the merged PR, matches an existing
server-projected deployment observation, and passes the live rollout verifier.
The observation records revision provenance, not historical application health.
The first matching revision observation is the measurement boundary; current
sync and health must also verify. Compare the immediately preceding 72h with
`[boundary, boundary + 72h)`, never the overlapping rolling daily digest.
Further deployments during observation make attribution UNKNOWN. Missing
traffic, classification coverage or a traffic criterion also holds the choice.
A worse target rate reports REGRESSED; a met observation criterion reports
ACCEPTED as a descriptive association, without a causal savings claim.

The digest includes explicit counters and citations. Observed list cost,
currently settled ledger cost and unknown reservation exposure are separate;
ledger amounts are observed as of digest collection, not reconstructed historical
balances. These operational measurements do not establish the seven-day CI and
revert evidence required for mature outcomes. No verdict writes factory policy,
merges a PR, bypasses review or creates a second scheduler.
