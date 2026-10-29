# Review

Review the whole finished PR diff once, with surrounding code where needed.
The reviewer must be independent of the author and reports findings without
editing, committing, pushing or running `ci`.

Check the stated goal/spec or maintenance policy under `AGENTS.md` "Merge
authority". Independent agent review satisfies the review step; routine human
sign-off is unnecessary. The dispatcher owns landing.

## Focus on failures

1. Correctness and security: name the concrete input or state that fails.
2. Repository boundaries and applicable build/deploy gotchas in
   `docs/agents/reference.md`. Judge the justification for any
   `ratchet-allow` exception.
3. Reachability: trace changed flags, environment variables and values to the
   code that consumes them. Confirm that another gate does not disable that
   path. Document intentionally staged behaviour where it is defined.
4. Validation: require evidence proportionate to changed behaviour, including
   regression coverage for actual bugs. Documentation and mechanical changes
   do not need invented behaviour tests.
5. Goal alignment and any explicit human hold or unresolved policy decision.

Skip formatting covered by automation, style preferences, speculative risks
and unrelated audits. Small diffs need small reviews.

## Report

List concrete findings by severity with file/line, failure scenario and why it
matters. Distinguish blocking defects from optional improvements; optional
suggestions do not delay landing. Say when there are no blocking findings.

Finish with a verdict and the reviewed head SHA. Fixes require checking the
changed portion and its effects; repeat the full review only if scope or
assumptions changed. The dispatcher confirms that the review, fixes and
required checks cover the current PR head.
