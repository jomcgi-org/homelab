1. projects/factory/reviewer/review/goal.go parses CommonMark blocks and validates each source marker before counting the item.
2. TestGeneratedCriteriaMatchRenderer in //projects/factory/reviewer/review:review_test compares generated lists with rendered HTML.
3. TestDiagramBeforeCriteriaAllowsWithdrawal checks that two criteria following a fenced diagram allow withdrawal of criterion 3. The goal check stores issueCriteria directly in GoalCheck.Counted. TestWithdrawalCriteriaKeepsExcludedBlockers checks that the rendered item count cannot withdraw saved findings for excluded Markdown.
4. The human gate holds this PR until jomcgi's APPROVED review covers its head. The goal check cannot see that review in the diff.
