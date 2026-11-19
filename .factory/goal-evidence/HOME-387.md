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
