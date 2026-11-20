1–2. projects/factory/reviewer/review/convergence_test.go exercises the current head's unclear verdict and the confirmation call's new negative verdict.
3. REVIEW.md (“Later heads”) and laterRule in projects/factory/reviewer/review/prompt.go specify when the service lowers severity. TestScopeLowersUnchangedCodeInsideAnEditedFile covers a finding without a line on an untouched file.
4, 6. projects/factory/chart/reviewer_test.py checks the connector selector and deployment setting.
5. TestGoalReasonsAPIServesTheSelectedReplyForAHead in //projects/factory/reviewer/review:review_test reads the reply identified by the completed check, then edits the issue and repeats the request before another review poll.
