# HOME-574 evidence

1. `TestSessionReadFailureWaits` in `projects/factory/reviewer/review/session_test.go` checks each failed read with one worker. A second PR completes while the first waits. `TestUnrecordedRunningTurnIsCancelled` polls the stored wait after the attempt timeout.
2. `TestUnrecordedRunningTurnIsCancelled` covers resumable, size-limited, legacy comment-filter and mismatched review-count sessions. It checks that the two heads run exactly two tasks across cleanup and a subsequent poll. Both tests run in `//projects/factory/reviewer/review:review_test`.
