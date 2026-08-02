---
name: improve-ambient
invoke: explicit
summary: Explicit improve loop for ambient
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# improve-ambient

On-demand feedback loop for Bosun's ambient Discord behaviour. Gathers ambient
activation episodes from Postgres and S3, classifies the worst against a fixed
taxonomy, and drafts PRs editing the responsible lever (the ambient prompt, the
agent-reply voice prompt, or the attention gate) or staging a channel directive,
each with evidence tied to specific episodes.

Sister skill to improve-recipes (agent-run mechanics) and improve-artifacts
(design quality). This one owns the human-reviewed code levers; the directive
autopilot (`chat/autopilot_job.py`, ADR chat/001) owns the autonomous,
self-validating directive levers in the background. Design rationale:
`docs/decisions/chat/001-improve-ambient-loop.md`.

## 1. Unit of analysis: the ambient activation episode

One episode is one ambient engage: a `chat.attention_decision` row with
`decision='engage'`. `gather` enriches each with its trigger message, the
reactions and human follow-up that landed in the same channel shortly after the
engage, and (best effort) the agent thread that ran. `episode_id` is the
`attention_decision.id`.

**Reaction linking: exact where known, heuristic fallback.**
`attention_decision.reply_message_id` records the Discord id of Bosun's reply to
an ambient engage (populated by the bot; see the reply-link change). When it is
present, reactions are matched EXACTLY on
`reaction_event.message_id = reply_message_id` and each record's `reaction_match`
is `"exact"`. When it is null (the agent thread-opening path, or engages from
before the column was populated), reactions fall back to a `channel_id` plus
time-after-engage window (`AMBIENT_WINDOW_MINUTES`, default 30) and
`reaction_match` is `"time-window-heuristic"`; overlapping episodes in that
channel can then share signal. Human follow-up is always window-based (there is
no exact "did a human follow up" link). The agent-thread match is likewise
heuristic: `claude_agent.agent_threads` carries no channel or trigger key, only
its own `session_id`, so an episode is tied to the nearest agent thread created
closest in time around the engage (`AMBIENT_AGENT_WINDOW_MINUTES`, default 5,
with 2 min of pre-engage slack for clock skew). The Opus
deep-read (`fetch-episode`) resolves ambiguity per episode; do not over-trust a
`time-window-heuristic` attribution without reading the slice, and carry
`reaction_match` into the eval's `signals` so the fidelity is recorded.

## 2. Taxonomy v1

Fixed vocabulary so runs are comparable over time. `taxonomy_version: 1`. Adding
a mode means bumping the version in this file by PR. Two families.

Engagement-timing (map to the attention gate, or a channel directive when
channel-local):

- **barged-in**: engaged where it was not wanted.
- **over-eager**: engaged too frequently for the channel's rhythm.
- **missed-cue**: ignored a clear invitation. Cross-check by sampling
  `decision='ignore'` rows in the same window and looking for a following human
  nudge that shows the bot should have engaged.
- **interrupted-thinking**: cut into a human still composing / mid-thought.

Reply-quality/outcome (map to the system prompt or the agent-reply prompt):

- **third-person-leak**: said "the agent" / spoke in third person about itself.
- **invented-link**: a URL / PR / file reference not present in the result.
- **wall-of-text**: padded or over-long for what was asked.
- **off-voice**: not Bosun's register.
- **under-delivery**: did not actually answer the ask.
- **ignored-by-humans**: no follow-up and no reaction (dead on arrival).
- **productive** (positive): a 👍 reaction, or the conversation continued
  usefully.

## 3. Lever routing table

| Failure mode(s)                                                              | Lever                          | File / mechanism                                                                                   |
| --------------------------------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------------- |
| voice, `wall-of-text`, `off-voice`, general verbosity, uniform across channels + people | ambient / base system prompt   | `projects/monolith/chat/agent.py` `build_system_prompt()`                                          |
| `third-person-leak`, `invented-link` on agent-delivered replies             | agent-reply concierge prompt   | `projects/monolith/chat/summarizer.py` `_build_agent_reply_prompt()`                               |
| `barged-in`, `over-eager`, `missed-cue`                                      | attention gate prompt + thresholds | `projects/monolith/chat/attention.py` `evaluate()` prompt, `ATTENTION_THRESHOLD`, `_RECENT_TAG_THRESHOLD` |
| any failure concentrated in one channel                                     | channel directive              | `projects/monolith/chat/directives.py` `propose_update()` (stage a proposal, do not edit code)     |
| friction tied to one person across channels                                 | personal style pref            | `projects/monolith/chat/directives.py` `set_style_pref()` (flag for the autopilot; see below)      |

The system prompt and agent-reply prompt shape voice/verbosity uniformly across
every channel and person. The attention gate governs whether to engage at all.
A channel directive diverges behaviour for one channel; a personal style pref
diverges it for one person.

## 4. Three-level routing rule (where a divergence protocol is required)

This is the skill's distinctive judgement. Cluster confirmed failures by both
`channel_id` AND `author_id` (both are on every gathered record):

- **(a) Across two or more channels, not tied to one person** -> global prompt
  or gate edit (a reviewed PR against `agent.py` / `summarizer.py` /
  `attention.py`).
- **(b) Concentrated in one channel, other channels fine** -> channel directive
  (stage a proposal via `propose_update()`; do not touch code).
- **(c) Tied to one person regardless of channel** -> personal style pref.

State this reasoning explicitly in the report: it is the answer to "where is a
divergence protocol required", and it is exactly the classification the PR 3
directive autopilot consumes.

## 5. Directive guard() gotcha

Proposed directive text must avoid the blocked keywords `tool`, `grant`, `acl`,
`permission`, `ambient`, `repo`, `push to`, `admin` (whole-word,
case-insensitive; keep this list in sync with `_GUARD_KEYWORDS` in
`directives.py`), or `propose_update` / `guard()` rejects it (returning a reason,
not silently). Phrase around them (directives
shape tone and interaction style only, never access). Channel directives are
propose-then-confirm: `propose_update()` stages an inactive row and a human 👍 in
Discord confirms it. This skill never auto-applies a directive. Personal style
prefs have no proposal flow at all (`set_style_pref` sets directly), so this
skill does NOT set them: it FLAGS per-person friction for the PR 3 autopilot,
which owns the apply-with-revert path.

## 6. Methodology

1. **Window.** Defaults to "since the last commit touching the lever files"
   (`chat/agent.py`, `chat/summarizer.py`, `chat/attention.py`), overridable with
   `--since <ISO8601>` or a single `episode_id` for a targeted "why did Bosun do
   that" analysis:

   ```bash
   git log -1 --format=%cI origin/main -- \
     projects/monolith/chat/agent.py \
     projects/monolith/chat/summarizer.py \
     projects/monolith/chat/attention.py
   ```

   The previous generation's window is between the prior two such commits; use it
   for the before/after aggregates in the PR body.
2. **Gather.** Run `gather --since <window>`. Skip episodes whose `has_eval` is
   already true at the current `taxonomy_version` (they do not need
   re-classifying).
3. **Rank.** Deep-read the worst K by a fluidity/productivity score: negative
   reactions (`net_reaction < 0`) and `barged-in` / `under-delivery` are worst;
   `ignored-by-humans` next; a 👍 or a continued conversation is good. Any episode
   with a non-empty `agent_result_error` is automatically in the worst set.
4. **Deep-read.** `fetch-episode <episode_id>` for the transcript slice around
   each selected episode (surrounding messages, reactions, agent result), read it
   for the actual failure signal, then classify against the taxonomy and write
   the eval via `put-eval`.
5. **Map and ship.** Map each confirmed mode to its lever (section 3) applying
   the three-level rule (section 4). Edit lever files and/or stage channel
   directive proposals.

**Minimum evidence.** Fewer than about 5 episodes in the window: report-only, no
PR ("not enough evidence yet").

**Signal fidelity.** Reactions on bot messages are ground truth where present.
Before `reaction_event` data has accrued, fall back to follow-up counts +
attention confidence as proxies and say so explicitly in the report.

**Why a reply was withheld.** A `withheld_reason` on each engage record
disambiguates the silent paths that used to look identical (a null
`reply_message_id`): `agent_thread` (routed to the goose guest, which threads),
`no_reply` (the model chose silence), `send_gate` (the post-generation gate
vetoed the drafted reply), `empty_reply` (the model produced no content), and
null when a reply was actually sent. Use it to measure gate behaviour directly:
the `send_gate` rate over engages is the before/after verdict on whether the
send-gate is over-vetoing, and a rising `no_reply` rate flags the model going
silent too eagerly. An engage that was withheld by `send_gate` is a candidate
misfire to deep-read (was that veto right?), the same way a `net_reaction < 0`
reply is. Records from before the column existed carry `withheld_reason: null`
even when suppressed, so scope rate claims to the current-generation window.

## 7. Write scope

PRs may edit `agent.py` / `summarizer.py` / `attention.py` (Joe reviews every
PR) and stage `channel_directive` proposals via `propose_update()`. Per-person
friction is FLAGGED for the autopilot, never set directly (there is no per-user
proposal flow). Every diff hunk cites at least one `episode_id`; an edit that
cannot cite a specific episode is dropped. Sanity-check every edited Python file
before opening the PR:

```bash
python3 -c "import ast; ast.parse(open('projects/monolith/chat/agent.py').read())"
```

Bump the monolith chart in the same PR (prompt/gate edits deploy via the
monolith image): `bazel/tools/git/bump-chart.sh projects/monolith`.

## 8. PR body template

Three sections, in order (sibling-identical shape):

1. **Before/after aggregates.** This window vs the previous generation:
   failure-mode histogram, negative-reaction rate (episodes with
   `net_reaction < 0` over total), barge-in rate, and the `withheld_reason`
   breakdown over engages (`send_gate` / `no_reply` / `empty_reply` /
   `agent_thread` / replied), computed from the eval JSON and the gathered
   records in each window. The `send_gate` rate is the direct measure of
   whether the send-gate is over-vetoing.
2. **Per-edit evidence rows.** For each diff hunk: `episode_id`, channel, the
   signals (attention confidence, reactions, follow-up), a transcript excerpt,
   and which diff line addresses it.
3. **Out of scope, observed.** Anything the evidence points at outside the three
   code levers and the channel directives (for example an upstream agent-run
   failure, which belongs to improve-recipes).

## 9. Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to the `ambient-evals/` key prefix (the eval JSON);
  never touch any other object.
- Infrastructure / env failures (an agent-run outage, a transient DB error) are
  reported, never diffed. Do not tune a prompt to work around an outage.
- Directive changes go only through `propose_update()` (channel) or, for
  per-person friction, are flagged for the autopilot, never set here.

## 10. Invocation

Optional argument: a lookback window, or one `episode_id` for a targeted "that
just went badly" analysis.

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
  gather --since 2026-06-28T00:00:00Z \
  < docs/runbooks/improve-ambient/scripts/improve_ambient_tool.py
```

`fetch-episode` is the same shape with the subcommand and an episode id:

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  fetch-episode <episode_id> \
  < docs/runbooks/improve-ambient/scripts/improve_ambient_tool.py
```

`put-eval` cannot take both the script and the payload over stdin, so it takes
the eval JSON base64-encoded as an argv argument instead of stdin (the script
itself is piped in over stdin):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <episode_id> <base64-encoded-eval-json> \
  < docs/runbooks/improve-ambient/scripts/improve_ambient_tool.py
```

## eval JSON contract

Written per episode via `put-eval <episode_id> <base64-payload>` to
`s3://artifacts/ambient-evals/<episode_id>.json`. Required keys:

- `episode_id`
- `channel_id`
- `author_id` (the trigger author, for per-person clustering)
- `taxonomy_version`
- `failure_modes`: list of `{mode, confidence, rationale}`
- `signals`: `{attention_confidence, became_agent, human_followups,
  same_author_replied, reactions: {"emoji": count, ...}, net_reaction}`
- `route`: `"chat"` or `"agent"`
- `classified_at`: ISO timestamp, from `date -u`
- `levers_ref`: git SHA of `origin/main` for the lever files at classify time

## After merge

Prompt and gate edits ship with the MONOLITH image: the merged PR's chart bump
rolls them out. Channel directive proposals land as inactive rows and take
effect on a human 👍 in Discord. The next `/improve-ambient` run's before/after
aggregates are the verdict on whether the change worked.

