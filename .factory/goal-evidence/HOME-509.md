1. In `projects/factory/connector/cmd/connector/main.go`, `run` initializes
   tracing before the dispatch loop calls `eng.Tick` and shuts the provider
   down on exit. `Init` in `connector/internal/tracing/tracing.go` (under
   `projects/factory`) installs the OTLP/gRPC batch exporter when an endpoint
   is set. Its `Start` helper uses `trace.WithNewRoot()` even when called with
   a poll context. `Engine.Tick` in `connector/dispatch/engine.go` brackets
   each cycle with `StartPoll` and `FinishPoll`. Poll attributes collect the
   work observed across the cycle, and individual events retain the association
   between an issue, run, PR, head, round and transition.
   `Store.SetRunState` in `connector/store/store.go` captures the run creation
   time and terminal transition. Its commit callback emits the run root and,
   for a connector-created round, a separate review-round root. The SQL
   projection and `emitUnit` show the source of each attribute. In
   `connector/dispatch/delivery.go`, the enqueue, dequeue, ejection and merge
   paths call `startQueueAction`. Its completion callback records the outcome
   and observed destination state on the queue root and triggering poll event.
   `Engine.step` records the persisted destination state, outcome, PR
   number, head SHA and round (`Engine.workAttrs`) when a poll advances an
   open run, so the poll root carries `poll.pr.number`, `poll.head.sha` and
   `poll.round`. `reconcileEnqueue` gives the lost-response enqueue path the
   same enqueue root, marked `enqueue.reconciled`.
2. `TestWorkSpans` in `projects/factory/connector/dispatch/delivery_test.go`
   uses the SDK in-memory exporter through `captureWorkSpans`. It drives
   delivery, enqueue, ejection and a revision through the engine. It asserts
   six poll roots, two run roots, one review-round root, one enqueue and one
   ejection. It checks the enqueue event's issue HL-7, run 1, PR 1, head
   sha-1, round 0, review-to-merging transition and outcome on a single poll
   event. It checks the run roots’ PR, head, round and previous state and
   observes the nonterminal dispatched-to-finishing transition. A rolled-back close must leave the exporter count unchanged.
   It also checks PR number, head SHA and round on the poll roots and their
   run events, and round on the merge-queue roots.
   `TestCanceledIssueIsDequeuedAndNotMovedToDone` checks enqueue, dequeue and
   merge roots in order on the cancellation path.
   `TestLostEnqueueResponseEmitsEnqueueSpan` checks the reconciled enqueue
   root and its attributes.
3. `TestWorkSpans` finishes a task with a sentinel secret and inspects the
   exported attributes, events and status of every recorded span for that
   sentinel. `Finish` in `projects/factory/connector/internal/tracing/tracing.go`
   uses a fixed error message when recording upstream failures.
4. Export stays off: the chart default `connector.traceEndpoint` is empty
   and `projects/factory/deploy/values.yaml` does not set it, so the deploy
   render is unchanged. Setting it is the Activation step in the head
   commit, after the HOME-508 gateway is live.
