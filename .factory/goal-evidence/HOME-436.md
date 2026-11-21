1. projects/factory/reviewer/review/goal.go parses CommonMark blocks and validates each source marker before counting the item.
2. TestGeneratedCriteriaMatchRenderer in //projects/factory/reviewer/review:review_test compares generated lists with rendered HTML.
3. TestDiagramBeforeCriteriaAllowsWithdrawal checks that two criteria following a fenced diagram allow withdrawal of criterion 3. The goal check stores issueCriteria directly in GoalCheck.Counted. TestWithdrawalCriteriaKeepsExcludedBlockers checks that the rendered item count cannot withdraw saved findings for excluded Markdown.
4. Compare the current PR head with the commit_id on jomcgi's APPROVED review in the GitHub reviews for PR #8440. Approval requires an external review.
