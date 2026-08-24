# Threat model maintenance

The aggregate index is [docs/THREAT-MODEL.md](../THREAT-MODEL.md); ADR
[security/007](../decisions/security/007-aggregate-threat-model-index.md)
is the rationale. This runbook is the procedure.

## Add a finding

1. File the issue with the label:

   ```bash
   gh issue create --label security-finding --title "<area>: <claim>"
   ```

   The body states what exists, what an attacker gets, and what done
   means.
2. If the domain has a `projects/<domain>/THREAT-MODEL.md`, update its
   attack-table row and section 5 ranking.
3. Re-rank `docs/THREAT-MODEL.md`: bold claim, issue link, one plain
   sentence. Update the "Reviewed against commit" line.

## Close a finding

Close the issue. Remove the index entry, and move the per-domain row's
status label in the same PR as the fix when there is one.

## Verify before writing

Both stale-claim bugs on 2026-08-24 came from trusting issue titles and
session memory over the code.

- Trace every claim to the code or live config. A control's status label
  (enforced prod / enforced dev / shipped off / designed / none) means
  you followed the flag to its consumer, not that a values file declares
  it.
- Grep for an existing `projects/<domain>/THREAT-MODEL.md` before
  writing "none yet".
- `grep deploy/values.yaml` before any "off by default" claim.

## Write a per-domain model

Copy the shape of
[projects/embervm/THREAT-MODEL.md](../../projects/embervm/THREAT-MODEL.md):
trust boundaries, adversaries, assets, one attack table per adversary
with status labels, ranked residuals, explicit exclusions. Stamp the
commit under the H1. Write one when a surface starts taking untrusted
input the baseline controls do not describe.

## Model review before sharing

Two independent reviewers caught six real errors in the index on
2026-08-24, including two wrong findings and a missed document. To
repeat:

1. One detached worktree per reviewer:

   ```bash
   git worktree add /tmp/claude-worktrees/tm-review-sol origin/<branch> --detach
   git worktree add /tmp/claude-worktrees/tm-review-ox origin/<branch> --detach
   ```

2. Sol (Codex, bills the OpenAI subscription):

   ```bash
   cat <spec> | bazel/tools/codex/dispatch.sh frontier /tmp/claude-worktrees/tm-review-sol -
   ```

3. ox-alpha (OpenRouter stealth model, via opencode):

   ```bash
   cd /tmp/claude-worktrees/tm-review-ox && opencode run -m openrouter/stealth/ox-alpha "$(cat <spec>)"
   ```

4. The spec: review-only with no file edits; verify every factual claim
   against the repository; flag language an outside engineer bounces
   off; report findings as blocker / should-fix / nit ending with a
   `VERDICT:` line.
5. Verify each finding against the code before applying it, then batch
   the accepted fixes into one PR. On 2026-08-24 every applied finding
   survived verification; apply none unverified.

## Language

[docs/writing.md](../writing.md), then read the draft back for the
shapes the grep cannot catch: self-narration, consequence clauses,
unexplained internal names.
