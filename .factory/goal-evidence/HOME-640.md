1. `projects/factory/connector/dispatch/delivery.go` polls merged PRs from each review binding. `TestUntrackedMergedReviewUsesPRIdentity` covers two PRs sharing a head, a review that completes after the merge cursor advances, and recovery after a writer restart. It checks the attempt journal for each external call.
2. `projects/factory/chart/files/itsaplan/factory.ts` commits the issue and the receipt for its project and finding key in one transaction. A repeated request returns the recorded issue ID.
3. `TestDeferredMergeFollowups/0` covers an empty review.
4. `//projects/factory/connector/dispatch:dispatch_test` covers creation, a lost create response, and restart.
