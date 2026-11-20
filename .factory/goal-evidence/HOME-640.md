1. `projects/factory/connector/dispatch/delivery.go` polls merged PRs from each review binding. `TestUntrackedMergedReviewUsesPRIdentity` covers two PRs sharing a head without a connector work item.
2. `projects/factory/chart/files/itsaplan/factory.ts` commits the issue and the receipt for its project and finding key in one transaction. A repeated request returns the recorded issue ID.
3. `TestDeferredMergeFollowups/0` covers an empty review.
4. `//projects/factory/connector/dispatch:dispatch_test` covers creation, a lost create response, and restart.
