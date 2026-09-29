# Review

How to review a finished PR diff, for any agent asked to. Review once per PR,
at the end, not per commit or sub-task. The reviewer must not be the agent that
wrote the diff.

You report what is wrong; you do not edit. A review that quietly fixes things is
a review nobody can audit, and the dispatcher needs your findings separately
from any change made in response to them.

## Scope

One comprehensive pass over the **whole** PR diff. You are not reviewing a
commit or a sub-task. Start with:

```bash
git diff origin/main...HEAD
```

Read the surrounding code for anything the diff touches. A diff that looks fine
in isolation and wrong in context is the most common thing a reviewer misses.

## What to weight

Ranked. Spend your effort at the top.

1. **Correctness.** Cases where the code produces a wrong result or crashes.
   State the concrete input or state that triggers it. A finding you cannot
   describe a failure path for is a guess, so drop it.
2. **The gotchas and gated rules in `AGENTS.md`.** CI catches most gated
   ones; judge the stated reason on any `ratchet-allow` opt-out in the diff.
3. **Config that looks live and is not.** For every flag, env var or values key
   the diff adds, changes or relies on, ask two questions: is anything consuming
   it, and is the branch that consumes it reachable? A key can be spelled
   correctly, genuinely read by the application, and documented in the README,
   and still be dead because a different flag disables the path that reads it.
   `TRUST_PROXY_AUTH` is inert whenever `MCP_CLIENT_AUTH_ENABLED` is true, and
   every mechanical check passes on it. Read the datapath, not the config.

   Two shapes to watch for. A setting that does nothing, which reads as the
   mechanism to the next person and sends them down a dead end. And a setting
   that is deliberately inert until some other work lands, which is legitimate
   but must say so where it is defined.
4. **Test coverage of the change.** New behaviour with no failing-then-passing
   test, or a changed numeric constant whose assertions were not updated.
5. **Simplification.** Only where it is a real reduction, not a rewrite in your
   preferred style.

Match the surrounding code's conventions rather than imposing your own. If the
file's idiom differs from your instinct, the file wins.

## What to skip

Formatting (prettier, ruff, gofumpt and buildifier run in CI), em-dashes (a hook
catches them), and anything the diff did not touch. Do not open a general audit
of the repo.

## Report back

Findings only, most severe first. For each: file and line, one sentence on the
defect, and the concrete failure scenario. Say plainly when a section is clean;
do not invent findings to look thorough. Close with a one-line verdict on
whether the change does what it set out to do.

Do not commit, push, or run `ci`. The dispatcher owns those.
