# Threat model maintenance

The model's structure is [docs/THREAT-MODEL.md](../THREAT-MODEL.md);
[docs/security.md](../security.md#findings-and-threat-model) carries the
rationale. This runbook is the procedure.
Issue [#5294](https://github.com/jomcgi-org/homelab/issues/5294) moved
per-domain models into STPA security lenses, superseding the per-domain
prose models the original decision called for; no architecture update is needed.

## Add a finding

Findings stay out of the public repository
([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-230#comment-2061)).

1. Record the finding in the private security project. State what exists,
   what an attacker gets and what done means.
2. File the fix in HOME in neutral words. Add a comment that names the
   private record.
3. Update the domain's security lens: copy `projects/<domain>/stpa/*.json`
   into a fresh `$STPA_TMP`, add or update the UCA, unsafe-feedback, or
   hazard row there (`status` and the HOME issue on every security row; a
   tracked unsafe state with no single attacking action goes on the hazard
   row), then run BLOCK A and BLOCK B from `docs/agents/stpa.md`. The blocks
   only read fragments from `$STPA_TMP` or `origin/main`, so an edit left in
   the working tree is never picked up. Never hand-edit `STPA.md`.
4. Update `docs/THREAT-MODEL.md` only when an asset, a boundary or a control
   changes. Update its "Reviewed against commit" line.

In the repository, state the rule and its enforcement. Do not state that a
control is absent in words that name a usable path.

## Close a finding

Close the HOME ticket and the private record. Move the security lens row's
status. Add the invariant row, or change it to `live`, in the PR that builds
the enforcement.

## Verify before writing

Both stale-claim bugs on 2026-08-24 came from trusting issue titles and
session memory over the code.

- A control named in a doc is not evidence; read the deployed object.
- Trace every claim to the code or live config. A control's status label
  (enforced prod / enforced dev / shipped off / designed / none) means
  you followed the flag to its consumer, not that a values file declares
  it.
- Grep `projects/<domain>/stpa/security.json` before
  writing "none yet".
- `grep deploy/values.yaml` before any "off by default" claim.

## Add a security lens to a domain

Follow `docs/agents/stpa.md` with the `security` lens for that system. It consumes
committed `stpa/structure.json`, produces `stpa/security.json`, and renders
the lens into `STPA.md`. Every security row requires `status`, which must be
one of `enforced-prod`, `enforced-dev`, `shipped-off`, `designed`, or `none`.
Add `issue` wherever the row is tracked. Include trust boundaries and
exclusions only when they map to the lens scope, losses, or hazards.

## Model review before sharing

Two independent reviewers caught six real errors in the index on
2026-08-24, including two wrong findings and a missed document. To
repeat:

1. One detached worktree per reviewer, each on a different model family
   from the author and from each other:

   ```bash
   git worktree add /tmp/claude-worktrees/tm-review-a origin/<branch> --detach
   git worktree add /tmp/claude-worktrees/tm-review-b origin/<branch> --detach
   ```

2. Run each reviewer in its worktree with the spec below, through the
   factory or any agent CLI directly. Which models is the factory's call;
   what matters is that they are independent of the author.
3. The spec: review-only with no file edits; verify every factual claim
   against the repository; flag language an outside engineer bounces
   off; report findings as blocker / should-fix / nit ending with a
   `VERDICT:` line.
4. Verify each finding against the code before applying it, then batch
   the accepted fixes into one PR. On 2026-08-24 every applied finding
   survived verification; apply none unverified.

## Language

[docs/writing.md](../writing.md), then read the draft back for the
shapes the grep cannot catch: self-narration, consequence clauses,
unexplained internal names.
