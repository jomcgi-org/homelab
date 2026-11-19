1. Replay source: PR 8260, commit `25ef1eb37693d9bc418a17c12946f881d241d0e6`.
   At that commit, `projects/platform/authentik/BUILD` loads
   `//bazel/tools/pytest:defs.bzl`; its `factory_group_binding_test` target
   supplies the factory blueprint glob. The macro sets `pytest_main = True`.
   In `projects/platform/authentik/factory_group_binding_test.py`,
   `test_factory_application_has_group_binding` checks each discovered app,
   and `test_removing_group_bindings_denies_application` exercises the same
   assertion after removing each app's group bindings. The replay passed all
   16 cases. The tree-based verdict rules are in
   `projects/factory/reviewer/review/goal.go` (`GoalPrompt`).
2. In the same replay, a claim that the target skips pytest is contradicted by
   the loaded macro's invocation. See `TestGoalPromptJudgesChecksByTheCasesTheIssueNames`
   for the unsupported-claim rule.
3. `TestGoalCheckPublishesOnlyServiceWording` in
   `projects/factory/reviewer/review/goal_test.go` checks the numbered private
   reason from the internal API and searches the public check, state, and
   comment for its private marker.

The omitted-patch regression is `TestGoalEvidenceReadAtHeadWhenPatchOmitted`.
It supplies the macro citation only through the immutable head file API and
checks its quoted representation in the outbound goal session. The separate
`TestGoalPromptQuotesEveryUntrustedLine` exercises hostile delimiter text.

### Replaying the evidence-only revision

The parent was the exact #8260 head above. The only new file was
`.factory/goal-evidence/HOME-351.md`. Its complete contents are the
`evidence` constant in `TestGoalCheck8260EvidenceOnlyRevision`. Adding it
to that parent produces Git tree `f2b000b23bfe64c86ebfc2dca44a462f0eb20ee7`
(check with `git write-tree`). The test records both head SHAs.

A read-only model review received the previous pytest finding, the three
criteria, and line-quoted contents from that tree: the evidence file, BUILD,
the loaded macro, the test, and every factory blueprint. It returned
`met` for criteria 1, 2 and 3 and `met` overall. It traced the pytest
entry point, identified positive group bindings for all six apps, and
verified that the removal case calls the same per-app assertion.

The Go regression replays that recorded verdict through the service after
an earlier blocking check. It checks that the revision prompt includes the
earlier finding and the new head's quoted evidence, and that the final
check stores all three verdicts and clears the blockers.
