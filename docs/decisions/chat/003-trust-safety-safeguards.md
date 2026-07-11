# ADR 003: Trust & Safety Safeguards (Ledger, Lockout, Shadow Forest)

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-11
**Relates to:** [001 - Ambient Feedback Loop](001-improve-ambient-loop.md), [agents/029 - Discord Bot Feature ACL](../agents/029-discord-bot-feature-acl.md), [agents/044 - Code Executor Sandbox](../agents/044-code-executor-sandbox.md)

---

## Context

The server's members treat Bosun as a standing red-team target: prompt
injection, exfiltration probes ("dump the system prompt", "paste the .env"),
permission fishing, mention flooding, and resource-exhaustion / OOM-bait
("calculate pi to 100 million digits") are a recurring game. The existing
defenses are structural (ACL allow-lists, the tone-only directive `guard()`,
fixed tool sets, the fail-closed attention gate) and hold up, but every attempt
still costs real resources: each probing message can trigger an attention
classify, a chat reply, or in the worst case a goose agent run. There is no
per-user memory of hostile behaviour, so the hundredth injection attempt is
processed as credulously (and as expensively) as the first.

The ask: detect bad actors from their behaviour, reduce their impact, and make
the enforcement legible (an emoji that says "seen, but you're locked out"),
with random-forest modelling as the detection engine. The honest constraint:
a forest needs labeled training data, and on day one there is none.

## Decision

One per-(guild, user) trust ledger (`chat.user_trust`), fed by three scoring
lanes of increasing cost, enforced at a single choke point at the top of
`on_message`, with every observation logged as a labeled feature-vector row
(`chat.moderation_event`) so the forest lane can be trained from real behaviour
rather than guesses.

### The ledger and soft lockout

Scores start at 100 and recover at `SAFEGUARDS_RECOVERY_PER_DAY` (20), so a
lockout is a cooling-off, not a ban. Signals subtract: heuristic pattern hits
(25 each, two counted per message), permission probes aimed at the bot (10),
resource-exhaustion / OOM-bait aimed at the bot (20), mention bursts (8),
LLM-judged malicious intent (up to 30, scaled by confidence). Below
`SAFEGUARDS_LOCKOUT_THRESHOLD` (40) the user is soft-locked:
no attention classify, no reply, no agent run, no message storage. When a
locked-out user addresses the bot directly, Bosun reacts with
`SAFEGUARDS_LOCKOUT_EMOJI` (default the anchor: "you're in the brig") instead
of replying, so the red team gets a legible signal that the message landed and
the gate held. Lurking while locked out is simply ignored (no emoji spam).
Everything fails OPEN for normal traffic: a ledger error can never block chat.

The owner is exempt and unledgered (admin chat is full of exactly the
vocabulary the patterns hunt, and owner rows would poison the training set).
`monolith-chat-trust-status` and `monolith-chat-trust-pardon` MCP tools expose
the ledger; a pardon resets the score AND flips the user's recent labels to 0,
turning a wrong lockout into corrective training data.

### Three lanes, one enforcement point

1. **Heuristics** (`chat/safeguards.py`, every message, no LLM cost): narrow
   regexes for instruction override, prompt/secret fishing, exfiltration
   verbs, persona jailbreaks, fake system frames, tool-scaffold smuggling;
   plus permission-probe, mention-burst, and resource-exhaustion checks. The
   resource-exhaustion signal (and the permission probe) only counts when the
   message is aimed at the bot, so ops chat about a real OOM and dev talk
   about an infinite loop stay clean, and it is tuned so a bounded ask ("pi to
   1000 digits") never fires. Deterministic, explainable, instant enforcement,
   and the source of positive labels.
2. **LLM intent** (classify-only Qwen call, same seam as the attention gate):
   fired-and-forgotten on bot-addressed, heuristic-flagged, or
   ambient-engaged messages, so it adds zero latency to replies; its verdict
   lands on the ledger for the next message. Catches phrasings regexes miss.
3. **Random forest** (shadow first): trained from the accumulated labeled
   events, evaluated on the hot path by walking JSON trees in pure Python.
   Shadow models stamp `rf_score` onto events for review and contribute
   nothing to enforcement; promoting a model to live (making high scores a
   ledger signal) is a manual `chat.trust_model.status` flip after comparing
   shadow scores against real outcomes. An rf-only signal never labels
   itself as training truth, so a live model cannot feed its own output back
   into the next fit.

### Firecracker-offloaded training, dependency-free inference

The trainer (`chat/safeguards_train_job.py`, weekly `safeguards-train`
CronWorkflow) ships the literal source of `chat/safeguards_forest.py` (numpy
only, standalone by construction) into the fc-invoke sandbox guest, which
already carries numpy transitively; the fit runs inside the microVM's 25s cap
(dataset and tree count are sized for it) and returns a JSON tree ensemble.
The monolith never gains an ML dependency: scikit-learn stays out of the
image, inference is a pure-Python tree walk (microseconds), and a sandbox
outage degrades to an in-process numpy fit in the ephemeral job pod. Training
skips until the dataset crosses minimum-sample gates: a forest fit on a
handful of rows memorizes noise, and the other two lanes enforce regardless.

### Alternatives considered

- **scikit-learn in the monolith**: rejected; a heavyweight dependency in the
  serving image for a model that must not even enforce until it has data.
- **LLM-only detection**: rejected as the sole lane; it costs a model call per
  message, and a flooding attacker could turn the detector itself into the
  resource drain. Heuristics enforce instantly and for free.
- **Hard bans / message deletion**: rejected; friends red-teaming is wanted
  behaviour, and the game works best when the gate is visible, recoverable,
  and non-destructive.

## Consequences

- Red-team attempts stop compounding: the first probes are answered normally
  (and logged), repeat offenders lose engagement and stop consuming attention
  classifies, replies, and agent runs until their score recovers.
- Every enforcement decision is auditable (`moderation_event` rows carry the
  signal names, deltas, feature vectors, and rf scores), and every mistake is
  correctable and self-labeling via pardon.
- The training set grows from real behaviour with no extra plumbing; the
  forest can be judged against shadow scores before it is ever allowed to
  act. The bootstrap circularity (heuristics mint the labels the forest
  learns from) is accepted and documented: pardons and clean samples are the
  corrective pressure, and promotion stays a human call.
- New knobs to forget about until needed: `SAFEGUARDS_MODE`
  (off/observe/live), threshold, recovery rate, emoji, clean-sample rate, and
  the `SAFEGUARDS_TRAIN_*` gates.
