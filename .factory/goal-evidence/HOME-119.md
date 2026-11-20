1. `TestAssessmentAfterDone/absent` in `projects/factory/connector/dispatch/activation_test.go` exercises completion without a measure.
2. `projects/factory/chart/files/itsaplan/factory.ts` locks the parent and commits creation and the receipt together. `projects/factory/tests/itsaplan/deferred.test.ts` discards the first response and retries the same key. `TestAssessmentAfterDone` also replays a rollback-abandoned filing after a writer restart.
3. `TestAssessmentAfterDone/activation` sets the two GitHub merge observations 48 hours apart, processes the second at the connector clock's window anchor, and checks that timestamp and the scheduled date on the parent's assessment.
4. `TestAssessmentAfterDone` sets completion at 23:00 UTC and checks the first UTC date after the three-day window.
5. `//projects/factory/connector/dispatch:dispatch_test` covers accepted, inconclusive, regressed, missing and stopped verdict delivery.
