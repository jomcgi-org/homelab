---
name: improve-safeguards
invoke: explicit
summary: Explicit improve loop for safeguards
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# improve-safeguards

On-demand feedback loop for Bosun's trust ledger. Gathers moderation decisions
from Postgres and S3, classifies the questionable ones against a fixed taxonomy,
and drafts PRs editing the responsible lever, each with evidence tied to specific
decisions.

The ledger (ADR chat/003, `projects/monolith/chat/safeguards.py`) has three
scoring lanes but no correction lane: the heuristics and the LLM screen mint the
labels, and the random forest trains to imitate them, so nothing discovers a
false positive the heuristics themselves produce. The only ground-truth signal
today is the manual `pardon` (reactive: someone must notice a wrong lockout).
This skill is the missing supervision.

## 1. Unit of analysis: the moderation decision

One decision is one `chat.moderation_event` row (`event_id` is its id). `gather`
enriches each with the message it scored (join `chat.messages` on the Discord
id), the user's current `chat.user_trust` row, and two review flags:

- **near_boundary**: `score_after` landed within `_BOUNDARY_BAND` (default 15)
  of the lockout threshold (default 40). The classifier's least-certain zone: a
  wrong call here is one signal away from a lockout.
- **cross_lane_disagreement**: the heuristic/LLM verdict (`label`) and the
  shadow forest (`rf_score`) disagree (a positive label with a low rf, or a
  clean label with a high one). The ambiguous calls most worth a human read.

`kind` distinguishes the lanes: `signal` (heuristic hit), `llm_intent` (LLM
screen), `clean_sample` (sampled benign, label 0), `lockout` (crossed below the
threshold), `pardon`/`enforcement` (markers).

## 2. Taxonomy v1

Fixed vocabulary so runs are comparable over time. `taxonomy_version: 1`. Adding
a mode means bumping the version in this file by PR.

Correctness verdicts (one per decision):

- **false-positive**: a benign message was flagged (`signal`/`llm_intent`), or
  worse, it contributed to a `lockout`. The highest-stakes error: a wrongly
  locked user is silently ignored and brig-reacted. Map to the pattern/prompt
  that fired.
- **false-negative**: a `clean_sample` (or unflagged message) whose content, on
  read, is real red-team behaviour the lanes missed. Map to a pattern/prompt
  gap.
- **over-penalty**: correctly flagged, but the `delta` was too harsh for the
  severity (a mild probe scored like a hard injection). Map to a weight.
- **near-miss-boundary**: the direction was right but the score landed fragile
  near the threshold in a way that a normal follow-up would wrongly tip. Map to
  a weight or the threshold. Informational unless it clusters.
- **mislabeled-training**: the row's `label` is wrong and will poison the forest
  regardless of enforcement. Map to a label correction (pardon/relabel), NOT a
  code edit.
- **correct-flag** / **correct-clean** (positive): the call was right. Recorded
  so the before/after aggregates have a denominator.
- **env-failure**: a ledger/DB fault, out of scope. Reported, never diffed.

## 3. Lever routing table

| Verdict / cause                                                   | Lever                        | File / mechanism                                                              |
| ---------------------------------------------------------------- | ---------------------------- | ---------------------------------------------------------------------------- |
| false-positive from an over-broad regex                          | heuristic pattern            | `chat/safeguards.py` `_INJECTION_PATTERNS` / `_PROBE_PATTERN` / `_RESOURCE_ABUSE_PATTERN` |
| false-positive from the LLM over-flagging                        | LLM intent prompt            | `chat/safeguards.py` `score_intent()` prompt                                  |
| false-negative (a real tell no pattern catches)                  | heuristic pattern OR LLM prompt | `chat/safeguards.py`                                                        |
| over-penalty / near-miss-boundary                                | signal weight or threshold   | `chat/safeguards.py` `_W_*` constants, `_DEFAULT_LOCKOUT_THRESHOLD`           |
| mislabeled-training (label wrong, poisons the forest)            | label correction             | `monolith-chat-trust-pardon` MCP (human-run), NOT a code edit                 |
| forest miscalibrated (rf_score consistently wrong)              | forest lane                  | report only; the forest retrains itself (`chat/safeguards_train_job.py`)       |

The heuristics and threshold gate whether the ledger flags at all; the LLM
prompt shapes the fuzzy calls the regexes miss; the weights shape how hard a
confirmed hit bites. A false positive that traces to one person's style, not a
pattern flaw, is not a code edit: note it (the owner is already exempt; a
per-user exemption is a ledger data change, not a lever here).

## 4. Enforcement-posture signal (the number that matters most)

The ledger ships in `live` mode (enforcing) by default. The single most
important aggregate is the **false-positive lockout rate**: confirmed-wrong
lockouts / total lockouts in the window. A rising rate is the signal to soften a
weight, raise the boundary margin, or recommend flipping `SAFEGUARDS_MODE` back
to `observe` until the lanes are tuned. Report it every run, even when no code
lever changes. A rising `pardon` count is the same signal seen from the human
side.

## 5. Methodology

1. **Window.** Defaults to "since the last commit touching the lever file"
   (`chat/safeguards.py`), overridable with `--since <ISO8601>` or a single
   `event_id` for a targeted "was that call right" analysis:

   ```bash
   git log -1 --format=%cI origin/main -- projects/monolith/chat/safeguards.py
   ```

   The previous generation's window is between the prior two such commits; use
   it for the before/after aggregates in the PR body.
2. **Gather.** Run `gather --since <window>`. Skip decisions whose `has_eval` is
   already true at the current `taxonomy_version`.
3. **Rank.** Deep-read the worst K first: every `lockout` and `pardon`, then
   `cross_lane_disagreement`, then `near_boundary`, then a sample of
   `clean_sample` rows to hunt false negatives, then any `has_resource_abuse`
   hit while that pattern is young. A confidently clean, unremarkable decision
   does not need a deep-read.
4. **Deep-read.** `fetch-decision <event_id>` for the scored message, the
   channel slice around it (banter vs a genuine probe), the user's recent ledger
   history (a lone hit vs a serial pattern), and reactions. Read it, judge the
   verdict against the taxonomy, and write the eval via `put-eval`.
5. **Map and ship.** Map each confirmed verdict to its lever (section 3). Edit
   only the responsible lever, and for `mislabeled-training` flag the row for a
   human pardon/relabel instead.

**Minimum evidence.** Fewer than about 5 reviewable decisions in the window:
report-only, no PR ("not enough evidence yet"). A single named `event_id` is
always a valid targeted analysis.

**Regression guard.** Before tightening a pattern to kill a false positive,
confirm the change still fires on the true positives in the window (and add a
paired test in `safeguards_test.py`). Before broadening one to catch a false
negative, confirm it does not start firing on the benign messages already in the
window. The `safeguards_test.py` paired positive/negative tests are the
contract; every pattern edit updates them.

## 6. Write scope

PRs may edit `chat/safeguards.py` (patterns, the LLM prompt, weights, the
threshold) with a paired `safeguards_test.py` update. Every diff hunk cites at
least one `event_id`; an edit that cannot cite a specific decision is dropped.
Label corrections are FLAGGED for a human `monolith-chat-trust-pardon`, never
performed here (Postgres is read-only). Sanity-check before opening the PR:

```bash
python3 -c "import ast; ast.parse(open('projects/monolith/chat/safeguards.py').read())"
```

Do not bump the monolith chart. Safeguards ship with the monolith image, and the
chart version for that is written back to main after the merge (ADR
platform/009 decision 1), so the PR carries no version.

## 7. PR body template

Three sections, in order (sibling-identical shape):

1. **Before/after aggregates.** This window vs the previous generation:
   verdict histogram (false-positive / false-negative / correct-* / ...), the
   **false-positive lockout rate**, the `pardon` count, and the cross-lane
   disagreement rate, computed from the eval JSON and the gathered records.
2. **Per-edit evidence rows.** For each diff hunk: `event_id`, kind, the message
   excerpt, the signals (heuristic names, delta, score_after, label, rf_score),
   the verdict, and which diff line addresses it.
3. **Out of scope, observed.** Anything the evidence points at outside the code
   levers (a per-person exemption, an enforcement-posture change, a forest
   retrain, or an upstream fault).

## 8. Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to the `safeguards-evals/` key prefix; never touch any
  other object or bucket.
- The skill never mutates the ledger. A pardon/relabel is a human MCP action it
  recommends, not one it performs.
- `env-failure` (a DB fault, a ledger outage) is reported, never diffed.
- Do not tune a lever to work around one person; the owner exemption and any
  per-user handling are ledger data, not a pattern edit.

## 9. Invocation

Optional argument: a lookback window, or one `event_id` for a targeted "was that
call right" analysis.

Find the monolith pod. There is no pod named "backend": the API lives in the
`backend` CONTAINER of the `monolith-*` pod (alongside frontend; the mesh is
Cilium now, so there is no linkerd-proxy sidecar):

```bash
kubectl get pods -n monolith -o name | grep '^pod/monolith-' | grep -v pg | grep -v atlas | grep -v searxng | head -1
```

Run a subcommand by piping the helper script over stdin into the pod's venv
python (note the `-c backend` container selector):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  gather --since 2026-07-11T00:00:00Z \
  < docs/runbooks/improve-safeguards/scripts/improve_safeguards_tool.py
```

`fetch-decision` is the same shape with the subcommand and an event id:

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  fetch-decision <event_id> \
  < docs/runbooks/improve-safeguards/scripts/improve_safeguards_tool.py
```

`put-eval` takes the eval JSON base64-encoded as an argv argument (the script
itself is piped in over stdin, so there is no second stdin channel):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <event_id> <base64-encoded-eval-json> \
  < docs/runbooks/improve-safeguards/scripts/improve_safeguards_tool.py
```

## eval JSON contract

Written per decision via `put-eval <event_id> <base64-payload>` to
`s3://artifacts/safeguards-evals/<event_id>.json`. Required keys:

- `event_id`
- `guild_id`, `channel_id`, `user_id`
- `kind`
- `taxonomy_version`
- `verdict`: one of the section 2 modes
- `confidence`: 0.0-1.0
- `rationale`
- `signals`: `{heuristic_signals, delta, score_after, label, rf_score,
  near_boundary, cross_lane_disagreement}`
- `lever`: `heuristic` | `llm-prompt` | `weight` | `threshold` |
  `label-correction` | null (null when the verdict is correct)
- `classified_at`: ISO timestamp, from `date -u`
- `levers_ref`: git SHA of `origin/main` for `chat/safeguards.py` at classify
  time

## After merge

Pattern/prompt/weight/threshold edits ship with the MONOLITH image: the merged
PR's chart bump rolls them out. A flagged label correction takes effect when a
human runs `monolith-chat-trust-pardon`. The next `/improve-safeguards` run's
before/after aggregates (especially the false-positive lockout rate) are the
verdict on whether the change worked.
