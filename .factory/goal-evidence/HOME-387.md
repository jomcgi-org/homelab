1. `TestGoalCheck8260EvidenceOnlyRevision` ("true claim") in
   `projects/factory/reviewer/review/goal_test.go` replays the PR #8260 head
   `25ef1eb37693d9bc418a17c12946f881d241d0e6` through the reviewer service.
   The first goal check repeats "pytest never runs". The revision adds only
   `.factory/goal-evidence/HOME-351.md`, which names
   `//bazel/tools/pytest:defs.bzl`; its check records criteria 1–3 `met` and
   clears the blockers. CI cannot run model inference, so `judge8260` stands
   in for the goal model. It follows the `GoalPrompt` evidence rules in
   `projects/factory/reviewer/review/goal.go`: it reads only the quoted
   evidence block and changes a verdict only when the cited BUILD file loads
   `py_test` from `//bazel/tools/pytest:defs.bzl` and the macro sets
   `pytest_main = True` in its checkout. Its `tree8260` checkout copies the
   #8260 lines that are on main today: `projects/platform/authentik/BUILD`
   (`load("//bazel/tools/pytest:defs.bzl", "py_test")` and target
   `factory_group_binding_test`) and `bazel/tools/pytest/defs.bzl`
   (`pytest_main = True`). The test does not claim a live model verdict.
   Joe decided that the live replay of #8260 on the deployed reviewer is an
   after-rollout check, and that this regression test is the evidence before
   merge ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-387#comment-4607)).
2. The "false claim" case of the same test keeps the evidence file but
   loads `py_test` from another file in the checkout. The finding stays open
   with an overall `not_met`.
3. `TestGoalCheckPublishesOnlyServiceWording` in
   `projects/factory/reviewer/review/goal_test.go` reads the numbered private
   reason from the internal API and checks that the public check, state and
   comment do not contain its private marker.

`TestGoalEvidenceReadAtHeadWhenPatchOmitted` checks that the service reads
the evidence file at the head SHA when GitHub omits its patch.
`TestGoalPromptQuotesEveryUntrustedLine` checks that hostile delimiter text
in the evidence stays quoted.
