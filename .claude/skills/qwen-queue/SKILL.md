---
name: qwen-queue
description: Operate and improve the qwen work-queue drainer. Use when queueing qwen-drain jobs, reviewing qwen-agent-for-review PRs, triaging failed drainer jobs, tuning job prompts, or when the user says "queue qwen work", "check the qwen queue", or "/qwen-queue".
---

# qwen queue

The drainer (ADR agents/061, `projects/monolith/swarm/drainer.py`) runs
`claude_agent.routine_jobs` rows of kind `qwen-drain` as one fresh qwen pi
session each, strictly serially, claimed by a `*/15` CronWorkflow tick. The
lane bills nothing, so it exists to convert idle overnight capacity into
audits, reports, and small PRs. This skill is the operating manual plus the
improvement loop.

## Queueing work

Register via MCP `monolith-agent-register-routine-job`:

- `kind: "qwen-drain"`, `payload: {"prompt": ...}` (optional `repo`, `branch`,
  `reasoning`).
- One-shot jobs: `interval_secs` null, `next_run_at` now. On completion
  `next_run_at` goes NULL and the job leaves the queue; `trigger-routine-job`
  re-arms it.
- Recurring jobs: set `interval_secs`. The same name cannot be registered
  twice; to edit a prompt, `deregister-routine-job` then re-register with the
  original schedule.

### Registering more than a handful

One MCP call per job does not scale past about ten. For a batch, call the same
function the MCP tool calls, in the pod, from a JSON file on stdin:

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

That is the real code path with its real validation, unlike writing SQL. 170
jobs registered this way in one call, zero failures.

**`next_run_at` is a priority field.** `claim_job` orders by `next_run_at ASC`,
so backdating a job to a fixed past timestamp puts it at the front of the queue.
Use it to run high-signal templates first, or to jump a spec'd PR job ahead of a
long audit backlog.

Job names must be unique forever: a completed one-shot keeps its row (so it can
be re-armed), so a re-run of the same audit needs a fresh prefix. Use a dated
batch prefix such as `qd0828-<template>-<path-slug>`.

### Sizing (the watchdog is the real budget)

The per-job ceiling is the EmberVM invoke watchdog at ~920s
(`piRuntimeWorkload.invocation.timeoutSeconds`), not the drainer's 1800s
timeout. A job must finish in **under 10 minutes of qwen time**. Repo-wide
sweeps blow it; per-file and per-directory questions fit. Split "audit all
runbooks" into one job per runbook.

### Thinking must stay on, and prompt quality will not save you if it is off

`agents.drainer.reasoning` defaults **true**, which sets `thinking: "high"` on
every drain session. Leave it on. The pi lane's own default is off because it
was built for small one-shot tasks, and drain jobs are multi-step audits.

This is worth stating because the symptom looks exactly like a prompt problem
and is not. With thinking off, qwen locks onto one tool call and repeats it
verbatim until the context window fills. Measured on the same prompt:

| thinking | tool calls | input tokens | outcome |
|---|---|---|---|
| off | 461 | 118787 | `stopReason: length`, no answer |
| on | 8 and 12 (two runs) | ~27000 | correct answer |

The prompt that looped 461 times satisfied every rule in the next section. Two
of the failures were plain report-only jobs, so task shape was not the trigger
either. Before rewriting a prompt that loops, check `DRAINER_REASONING` in the
pod env.

`PI_MAX_IDENTICAL_TOOL_CALLS` (20, in the EmberVM shim) is a backstop that ends
a turn after 20 consecutive byte-identical tool calls. It bounds the damage; it
does not make the job succeed.

### Prompt shape that works

qwen is a small model. Prompts that one-shot share these properties:

- **One bounded question** with an explicit output contract ("output one line
  per finding as 'file: problem'", "reply with exactly X").
- **Exact numbered steps** for multi-step work (the PR recipe below). Do not
  ask it to plan.
- A **verification clause**: "verify each claim with an actual grep/ls before
  reporting it". Without it, qwen asserts from memory.
- A **size cap** ("keep the whole answer under 1800 characters"): summaries
  land in `last_summary` and Discord.
- For report-only jobs, open with "Report-only task, do not modify files."
- "Never use em dashes" in anything that lands in commits or PRs.

## The PR lane

qwen sessions can open PRs with no extra plumbing: pi guests sit in the egress
lane, the sidecar injects the GitHub token host-keyed (Basic for git push,
Bearer for `api.github.com`), and the guest image ships git, gh, curl, jq.
The working recipe, verified by PR #5333:

1. Find the checkout (`ls /session /session/*` then cd to the dir with
   `.git`).
2. Make the exact edit (spell it out; file, line, old text, new text).
3. `git config user.name "qwen-drainer"` and a noreply email.
4. Branch `qwen/<job-name>`, Conventional Commit.
5. `git remote set-url --push origin https://github.com/jomcgi/homelab.git`
   (the clone origin is the node-local mirror, which cannot receive pushes).
6. `export GH_TOKEN=placeholder` (gh refuses to run without one; the sidecar
   replaces whatever it sends).
7. `gh pr create` with title prefixed `[qwen]`, label `qwen-agent-for-review`,
   the job name in the body. Never enable auto-merge.
8. "Your final answer must be only the PR URL."

**Tell the job NOT to touch `knowledge/repo_docs_manifest.ndjson`.** Every `.md`
under `docs/` or `projects/` feeds it, so a doc edit always changes it, but CI's
Format stage regenerates and auto-commits it as `ci-format-bot` (see the
docstring in `knowledge/tools/gen_repo_docs_manifest.py`). Verified on #5383 and
again on #5404: the PR gets a second commit `style: auto-format` carrying
`repo_docs_manifest.ndjson +1/-1`. Asking qwen to regenerate it is extra work
that can go wrong; asking it to leave the file alone cannot.

The cost of that auto-commit shows up at merge time. The manifest is one JSON
object per line, so **two doc PRs open at once conflict on it**. Four qwen doc
PRs merged cleanly in sequence on 2026-08-29; the fifth sat long enough for main
to move and went `DIRTY`. The fix is not to resolve the conflict: reset the
branch to `origin/main`, re-apply the one-line edit, drop the stale format-bot
commit, and force-push. CI regenerates the manifest again.

### The audit-and-fix template

One file in, either `CLEAN` or a PR out. Prefer this over report-only for code
and doc changes: a report costs a dispatcher round-trip to become a fix, while a
PR arrives reviewable.

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
    --push origin https://github.com/jomcgi/homelab.git; push; export
    GH_TOKEN=placeholder; gh pr create with title prefixed [qwen], label
    qwen-agent-for-review, and a body containing the evidence block.

    Verify with git diff --stat before pushing. It must show ONLY {target}.
    If it shows any other file, run git checkout -- . and reply EDIT FAILED.
    Do NOT edit projects/monolith/knowledge/repo_docs_manifest.ndjson; CI
    regenerates it.

    Final answer: the PR URL, or CLEAN, or a list of reported-not-fixed lines.

The evidence block is what makes the Opus review cheap: the reviewer checks
whether the two quoted sides actually disagree, rather than re-deriving the
finding.

### Review policy

Review keys on diff class, not author.

- **Spec'd job** (the dispatcher wrote the exact edit): dispatcher checks the
  diff matches the spec, PR CI gates, no separate Opus pass. There is nothing
  to judge, because the finding was verified before the job ran.
- **Audit-and-fix job** (qwen found the drift itself): **one Opus review**.
  Nobody has judged the finding yet, so the review is judging both the claim
  and the edit. Read the evidence block in the PR body first; if the two sides
  it quotes do not actually disagree, close the PR rather than fixing it.

A human merges; qwen PRs are never auto-merged.

The review gate is what makes audit-and-fix affordable. A false positive costs
one review, not a bad merge. But it is still a real cost: a 174-job batch
produced 89 findings, so cap a batch at what you are willing to review in one
sitting rather than queueing every candidate at once.

## Inspecting outcomes (the improvement loop)

0. **Open the drain console first.** `/private/agents/drain` in the UI, or
   `GET /api/agents/drain/console` in-pod. It classifies lane state from
   `dbos.operation_outputs` step checkpoints (running under 120s of silence,
   quiet under 600s, wedged beyond that, plus a distinct stranded state for
   ENQUEUED rows on a stale `application_version`), shows the per-job tool-call
   fingerprint, and offers cancel and requeue inline. It exists because the
   alternative is querying Postgres by hand, which is how a 39-minute wedge
   went unnoticed on 2026-08-29.

   Read its `state`, not its `age_seconds`: `age_seconds` is the CYCLE age,
   while the wedge verdict comes from time since the last step checkpoint. A
   cycle can legitimately sit in `start_agent_session` for up to 19 minutes
   while `create_session` walks the capacity backoff ladder, so "quiet" there
   is normal and the console will say `wedged` before the reaper acts.

1. `monolith-agent-list-routine-jobs` with `kind: "qwen-drain"`:
   `last_status` and `last_summary` are the per-job verdicts.
2. `gh pr list --label qwen-agent-for-review` for the PR backlog (the
   daily-repo-pulse job also reports this).
3. For each failure, classify against the known taxonomy below. Check the
   HARNESS first: on a 174-job batch every single failure was infrastructure
   (bad guest, deploy roll, watchdog), none was qwen producing a wrong answer.
4. Fold any new failure mode into this skill in the same PR that fixes its
   first occurrence.

**Do not triage by regex.** The output contract asks for "your entire final
answer must be exactly CLEAN" and **no job has ever honoured it**: 174 of 174
wrapped the verdict in prose ("CLEAN / Verification summary...", "Verdict: NOT
CLEAN"). So `last_summary NOT LIKE '%CLEAN%'` silently classifies "NOT CLEAN" as
clean, and the inverse counts every job that merely uses the word. Read the
summaries, or match on a finding shape such as `'\.md:[0-9]+: doc says'`.

**A finding is not a defect.** Two false-positive classes are systematic, look
identical to real findings in a list, and will waste a PR round each:

- **Stale-but-working references.** 32 files still say `jomcgi/homelab` after
  the org move. GitHub redirects it, the qwen PR recipe itself uses it, and
  every PR pushes fine. Churn with no functional gain.
- **Comparisons the template got wrong.** A doc citing the caller-facing MCP
  tool name (`monolith-monolith-agent-trigger-job`, double prefix from FastMCP)
  against the Python function name (`monolith_agent_trigger_job`) is not drift.
  Nor is a README that does not enumerate every file in its directory, when it
  never claimed to.

Verify by category rather than one by one: the failures are systematic, so a
sample of each template tells you whether that whole class holds.

### Failure taxonomy

| Symptom in `last_summary` | Cause | Fix |
|---|---|---|
| `terminal_reason` `length`, ~119k input tokens, hundreds of tool calls | the lane ran with thinking OFF and the model repeated one identical call until the context filled | should not recur: `agents.drainer.reasoning` now defaults true. If it does, check `DRAINER_REASONING` is actually set in the pod env |
| literal `<tool_call>` as the whole summary | 15 of 1035 turns leaked this: 11 truncated by token limit (already fail on own, `terminal_reason: length`), 4 complete but malformed with spurious junk closing tags (silently pass as `ok`, `terminal_reason: stop`). NInfer's parser rejects the junk and returns raw XML as content. Guard now detects and fails both variants. | reduce job size if truncated; malformed is NInfer parser issue |
| `502 :invoke_timeout` after ~15 min | the 920s EmberVM invoke watchdog | usually a non-converging job; check the tool-call count in `usage_json` before assuming the job is oversized |
| `502 {:session_down, ...}`, `503 workspace does not exist`, `All connection attempts failed`, `Server disconnected` | the GUEST was bad, the job was fine | `trigger-routine-job` and it will almost certainly pass on a fresh guest. Measured 2026-08-29: five such failures, five clean passes on requeue. The drainer treats all of these as terminal, so a one-shot dies permanently unless you requeue it by hand |
| jobs due but nothing claimed for hours, ticks firing | a wedged drain_cycle holding the concurrency-1 slot (#5328) | open the drain console (below). A monolith pod roll does **NOT** clear it: DBOS recovery re-enqueues into a permanently PENDING row. The reaper now cancels a cycle with no step checkpoint for 1800s; to clear one sooner, `POST /api/swarm/runs/{workflow_id}/cancel` in-pod |
| `next_run_at` NULL, job vanished from due list | one-shot completed; this is normal | `trigger-routine-job` to re-run |

### Harness knobs

`agents.drainer.*` in `projects/monolith/chart/values.yaml`: `enabled` (the
kill switch, flipped in deploy values), `maxJobsPerCycle`, `turnTimeoutSeconds`
(1800), `reasoning` (true; see the thinking section above),
`stallThresholdSeconds` (advisory only), `jobKind`, `repo`, `branch`.
Session-side limits live in `piRuntimeWorkload` in
`projects/embervm/deploy/values.yaml`.

**Read the knob from the deployment, not from `agent/config.py`.** The code
default for `maxJobsPerCycle` is 3 and the chart sets 15. A calibration pass on
2026-08-29 read the code default and produced a throughput estimate five times
too pessimistic ("2 to 4 days" for a batch that finished in about nine hours).

    kubectl get deploy monolith -n monolith -o jsonpath='{range .spec.template.spec.containers[?(@.name=="backend")].env[*]}{.name}={.value}{"\n"}{end}' | grep DRAINER

Throughput: a cycle claims up to `maxJobsPerCycle` jobs and runs them serially,
and chains straight into a successor when it hits that bound with at least one
success, so a deep backlog drains continuously rather than in 15-minute bursts.
Measured on a 174-job batch: about 1.8 to 3.6 minutes per job depending on
template weight, roughly nine hours end to end. Over-filling is harmless because
unclaimed jobs simply wait.

## What qwen is for (and not for)

Good: per-file staleness audits, path-citation checks, TODO inventories,
index-vs-directory drift, single-defect spec'd PR fixes, daily digests.
Bad: anything needing bazel or the test suite (not in the guest), repo-wide
sweeps, judgment calls on prose, multi-file refactors, anything where a wrong
answer is expensive to detect. The dispatcher owns finding defects worth
fixing; qwen owns bounded execution.

**Task shape is the strongest predictor in this lane.** Recorded to date:

| shape | record |
|---|---|
| spec'd single-edit PR (dispatcher names file, anchor, replacement) | **7 of 7** |
| bounded report-only audit | 174 of 174 completed |
| discovery plus PR in one job | **0 of 6** |

What made the 0-of-6 shape fail was **unbounded** discovery, not the
find-and-fix combination. "Find one wrong thing somewhere in `projects/mcp/`,
then fix it" has no stopping rule, so the job explores until something kills it;
one burned 434 tool calls without reaching the fix. All six also ran with
thinking off, which is what turned exploration into a loop, so that evidence is
confounded.

So the rule is about the SEARCH SPACE, not the combination:

- **One named file, then fix it: allowed.** The discovery is already bounded by
  the file, and the PR carries its own evidence for review.
- **A directory or "somewhere in the repo", then fix it: never.** Report only,
  and let the dispatcher pick what is worth fixing.

An audit-and-fix job must also **report rather than edit** when the fix needs
judgement: superseded ADR wording, whether a doc is aspirational or describes
something lost, anything touching more than one line. `docs/security.md`
describing a Kyverno layer that is not deployed is the canonical example, since
the fix is either a docs rewrite or restoring the policies and only a human
picks.

The two-phase route (report, dispatcher verifies, spec'd job edits) stays
correct for anything the dispatcher already knows it wants fixed, and for
findings that came out of a report-only sweep.

Three habits make the spec'd job reliable:

- **Verify the anchor is unique** (`grep -c`) before writing the spec. If it
  appears twice, say so and state the expected diffstat as 2 insertions and 2
  deletions, or the self-check below aborts a correct edit.
- **Include a `git diff --stat` self-check** that reverts and replies `EDIT
  FAILED` on any unexpected diffstat. It has caught nothing yet, which is the
  point: it makes a wrong edit loud instead of silent.
- **Verify the replacement value at source yourself.** A finding that says "doc
  says 15s but code says 90s" is a claim; open the file and confirm before
  putting the number in a spec.

qwen will also tell you when your premise is wrong rather than inventing an
answer: it reported a nonexistent `projects/monolith-public/frontend`, found the
real path when the spec named a wrong `projects/shared/chart/templates`, and
switched to the GitHub API when its checkout came up empty. Read those replies
as spec bugs, not job failures.
