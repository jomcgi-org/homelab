---
name: refresh-structure-docs
invoke: explicit
summary: Freshen root README structural prose and open a PR
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

Freshen the top-level `README.md` so its prose matches what the repository has
actually become, and open a PR with any change. This is the judgment half of a
two-part scheme:

- **CI guardrail (deterministic, already enforced):**
  `bazel/tools/format/readme_structure/check_readme_structure.py` fails the
  "Format check" action on a link to a deleted project or a new top-level
  `projects/*` dir mentioned nowhere. You do not need to re-police those; CI
  already blocks them at merge.
- **This skill (judgment):** the staleness a test cannot decide, a wrong model
  name, a renamed subsystem, a count that has drifted, a new *major* project
  worth promoting into the layout tree, a description that no longer fits.

Land every change as a PR against `main`. **Never auto-merge** (unlike the STPA
routine): README prose is a human-voice artifact and Joe reviews it.

## Method

1. **Read the current README and the mechanical status.** Read `README.md`.
   Run `python3 ./bazel/tools/format/readme_structure/check_readme_structure.py`
   from the repo root. A non-zero exit means the deterministic check is already
   red on `main` (rare, since CI blocks it); if so, fix that first, it is the
   highest-value edit.

2. **Diff each structural claim against reality.** Work through the README
   section by section and verify against the tree, not memory:
   - **Systems / Applications lists:** does each linked path still exist and
     still do what the blurb says? `ls` the target. Has a headline system been
     added (a new `projects/*` with its own chart/deploy and a real story) that
     deserves a bullet?
   - **Repo layout tree:** does every entry still exist? Is a genuinely major
     new project missing? The tree deliberately shows "major dirs" only, so do
     NOT list every project, promote only ones that have become load-bearing.
   - **Infrastructure patterns table:** spot-check the volatile cells, model
     name and serving stack (grep `inference/` values and the actual deployed
     model), CI/GitOps/mesh tooling names. These drift silently.
   - **Counts and superlatives** ("N apps", "the big one"), confirm or drop.

3. **Make the smallest honest edit.** Change only what is wrong. Do not
   restructure, re-voice, or churn correct prose, a large diff is a signal you
   overreached. Match the existing terse, declarative register. **Never write
   an em-dash**; use a comma, colon, parentheses, or split the sentence (repo
   rule, a `check-commit-msg-ascii.sh` gate rejects them in commit messages and
   a non-ASCII em-dash in copy is off-voice regardless).

4. **If nothing is stale, stop.** Do not open an empty or cosmetic PR. When the
   README is accurate, say so and exit. A no-op week is the expected common case.

5. **Land the change as a PR** (only if you edited something):
   ```bash
   git switch -c chore/refresh-structure-docs-$(git rev-parse --short HEAD)
   git add README.md
   git commit -m "docs: refresh README structure to match repo"
   git push -u origin HEAD
   gh pr create --fill --base main \
     --title "docs: refresh README structure to match repo" \
     --body "Weekly structure-docs refresh. What changed and why:\n<one line per edit>"
   ```
   Use rebase merge only, and only after Joe reviews. Do not enable auto-merge.

## Scope guardrails

- **Root `README.md` only.** Per-project READMEs are out of scope; if one looks
  badly wrong, note it in the PR body rather than editing it here.
- **Prose, not structure enforcement.** Adding a new project to satisfy the
  coverage check is the author's job in their own PR (or a one-line
  `COVERAGE_ALLOWLIST` entry); do not paper over a genuinely undocumented new
  project just to green the check, surface it.
- **One PR per run, small diff, no auto-merge.**

