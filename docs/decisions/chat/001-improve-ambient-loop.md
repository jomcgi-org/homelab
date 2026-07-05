# ADR 001: Ambient Feedback Loop and Directive Autopilot

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-05
**Relates to:** [services/002 - Discord Chat Automation](../services/002-discord-chat-automation.md)

---

## Context

Bosun engages ambiently in Discord: an attention gate (`chat/attention.py`)
decides engage or ignore on non-mention messages, and an engage either replies
in-monolith or runs a goose agent whose result is delivered back in Bosun's
voice. Two failure classes recur. The gate mistimes: it barges into a channel
that did not want it, or misses a clear invitation. The reply misfires: it leaks
third person ("the agent found..."), invents a link the result never contained,
runs long, or drifts off Bosun's register.

Until now the only quality signal was inferring from follow-up text, which is
weak and ambiguous. We added `chat.reaction_event` so a human 👍/👎/🤖 on
Bosun's own reply becomes ground truth. With attention decisions, reactions, and
follow-up, each ambient activation is now a scorable episode. The question this
ADR answers is how to turn those episodes into improvement without either
letting a model rewrite the bot's prompts unsupervised or drowning Joe in a
per-channel tuning chore.

## Decision

Split the loop by the reversibility and the verification latency of each lever.

**Code levers stay human-reviewed (the `/improve-ambient` skill).** The system
prompt (`chat/agent.py` `build_system_prompt()`), the agent-reply voice prompt
(`chat/summarizer.py` `_build_agent_reply_prompt()`), and the attention gate
(`chat/attention.py` `evaluate()` prompt plus `ATTENTION_THRESHOLD` and
`_RECENT_TAG_THRESHOLD`) shape every channel and every person at once. A bad
edit there is a global regression, and its only cheap catch is a human reading
the diff. So these change exclusively through an offline, evidence-backed skill
that mirrors `/improve-recipes` and `/improve-artifacts`: gather episodes
read-only, deep-read the worst with Opus judgement, map confirmed failures to a
lever, and open a PR where every diff hunk cites at least one episode id. Joe
reviews every such PR. This is the on-demand, human-reviewed half.

**Reversible directive levers go autonomous (the directive autopilot, a
forward-referenced background job).** Channel directives (`chat/directives.py`)
and per-user style prefs are prepended system-prompt text scoped to one channel
or one person. They cannot grant a capability, and a bad one degrades tone in a
single scope, not the whole bot. Crucially their quality signal returns within
minutes: the next few reactions and follow-ups in that scope say whether the
directive helped. That fast, scoped, reversible feedback is exactly what makes
an autonomous loop safe here and unsafe for the code levers. A background job
may therefore apply a high-confidence directive silently, baseline the scope's
score, and self-validate against the next window's reaction delta, reverting on
regression. Code levers never go through this path.

### Versioned taxonomy

Episodes are classified against a fixed, versioned vocabulary (taxonomy v1)
carried in the skill, so runs are comparable over time and a change to the
vocabulary is itself a reviewed edit. Two families: engagement-timing
(`barged-in`, `over-eager`, `missed-cue`, `interrupted-thinking`) and
reply-quality/outcome (`third-person-leak`, `invented-link`, `wall-of-text`,
`off-voice`, `under-delivery`, `ignored-by-humans`, `productive`). A stable
taxonomy is what lets the cheap in-monolith classifier the autopilot uses
inherit a vocabulary the Opus deep-read has already proven on a real window.

### Three-level routing rule

The distinctive judgement of this loop is deciding where a divergence protocol
is required, rather than reflexively editing a global prompt. Confirmed failures
are clustered by both `channel_id` and `author_id`:

- A mode present across two or more channels and not tied to one person is a
  global problem: edit the prompt or gate through a reviewed PR.
- A mode concentrated in one channel while others are fine is a channel problem:
  stage a channel directive, do not touch code.
- A mode tied to one person regardless of channel is a personal problem: a
  personal style pref for that user.

This routing is also precisely what the autopilot consumes: the per-scope
clustering is the classification, and only the channel and personal branches are
eligible for autonomous application.

### Silent background, introspection surface instead of announcements

The autopilot never posts in any channel, ever. Announcing "I have adjusted my
tone here" is itself noise of exactly the kind the loop exists to reduce, and it
invites bikeshedding on every micro-adjustment. Instead the current directives
and the autopilot's own apply/keep/revert provenance are exposed out-of-band on
an introspection surface (MCP tools over the directive history and the autopilot
log) for review and manual tuning. Visibility is pull, not push.

### Source precedence: manual wins

Both directive tables carry a `source` (`seed`/`observer`/`autopilot`/`manual`).
A human manual tune always beats the autopilot: the autopilot will not override
an active `manual` row within a cooldown, and when it finds one where it wanted
to act it records the intent for the introspection surface rather than applying.
Out-of-band human tuning is therefore authoritative, and the autopilot fills the
gaps it leaves.

### guard() keyword constraint

Every proposed or applied directive passes `directives.guard()`, which
keyword-blocks text that reads as if it changes tools, grants, ACLs,
permissions, ambient scope, repos, or admin. Directives shape tone and
interaction style only. The autopilot inherits this screen unchanged; it is a
hard precondition on any autonomous write, which is part of why only directives,
and never code, are eligible for autonomy.

## Consequences

- Two loops share one taxonomy and one episode model, so the autopilot rides a
  classifier the reviewed skill has validated rather than an unvetted one.
- The blast radius of autonomy is bounded to reversible, self-validating,
  single-scope text, and every global change still passes a human reading the
  diff.
- Directive drift is auditable but quiet: nothing lands in a channel, and the
  full apply/revert history is queryable on demand.
- Reactions are ground truth only where they have accrued. Early on the loop
  leans on follow-up and attention-confidence proxies and says so; fidelity
  grows as `reaction_event` fills in.
