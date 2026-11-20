1. projects/factory/reviewer/review/goal.go parses CommonMark blocks and validates each source marker before counting the item.
2. TestGeneratedCriteriaMatchRenderer in //projects/factory/reviewer/review:review_test compares generated lists with rendered HTML.
3. TestIssueCriteriaCount in the same target covers the Mermaid diagram and numbering gaps.
4. Compare the current PR head with the commit_id on jomcgi's APPROVED review in the GitHub reviews for PR #8440. Approval requires an external review.
