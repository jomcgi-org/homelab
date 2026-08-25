# Threat model maintenance

The aggregate index is [docs/THREAT-MODEL.md](../THREAT-MODEL.md); ADR
[security/007](../decisions/security/007-aggregate-threat-model-index.md)
is the rationale. This runbook is the procedure.
Issue [#5294](https://github.com/jomcgi-org/homelab/issues/5294) moved
per-domain models into STPA security lenses. ADR security/007 decision 3 is
superseded, as recorded in the issue, so no new ADR is needed.

## Add a finding

1. File the issue with the label:

   ```bash
   gh issue create --label security-finding --title "<area>: <claim>"
   ```

   The body states what exists, what an attacker gets, and what done
   means.
2. Edit `projects/<domain>/stpa/security.json`: add or update the UCA or
   unsafe-feedback row with `status` and `issue` fields. Re-render `STPA.md`
   with the `stpa` skill; never hand-edit `STPA.md`.
3. Re-rank `docs/THREAT-MODEL.md`: bold claim, issue link, one plain
   sentence. Update the "Reviewed against commit" line.

## Close a finding

Close the issue. Remove the index entry, and move the security lens row's
status label in the same PR as the fix when there is one.

## Verify before writing

Both stale-claim bugs on 2026-08-24 came from trusting issue titles and
session memory over the code.

- Trace every claim to the code or live config. A control's status label
  (enforced prod / enforced dev / shipped off / designed / none) means
  you followed the flag to its consumer, not that a values file declares
  it.
- Grep `projects/<domain>/stpa/security.json` before
  writing "none yet".
- `grep deploy/values.yaml` before any "off by default" claim.

## Add a security lens to a domain

Run the `stpa` skill with the `security` lens for that system. It consumes
committed `stpa/structure.json`, produces `stpa/security.json`, and renders
the lens into `STPA.md`. Every security row requires `status`, which must be
one of `enforced-prod`, `enforced-dev`, `shipped-off`, `designed`, or `none`.
Add `issue` wherever the row is tracked. Include trust boundaries and
exclusions only when they map to the lens scope, losses, or hazards.

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
