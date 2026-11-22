# Review-round cap

At the review-round cap (`maxReviewRounds`) no person decides the leftover
findings ([HOME-756](https://factory-plan.jomcgi.dev/factory/issue/HOME-756),
[Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-756#comment-10150)):
medium findings become follow-up issues and stop blocking, and a critical or
high finding starts a re-cut in a fresh coder session.

```mermaid
sequenceDiagram
    participant C as Connector
    participant G as GitHub PR body
    participant R as Reviewer
    C->>C: work item spends maxReviewRounds
    C->>G: add review-cap marker (factory App edit)
    R->>G: read body, author and last editor in one query
    alt marker in a body the factory App wrote last
        R->>R: medium findings become Deferred
        R-->>C: check passes, or fails on critical or high only
    else no marker, or another editor
        R-->>C: normal verdict
    end
```

## One count, owned by the connector

The connector counts review rounds per work item; the reviewer does not count
its own failures. When `deferAtReviewCap` is on and a work item has spent
`maxReviewRounds`, the connector adds `github.ReviewCapMarker` to the PR body.
`prBody` writes it on every later sync, and `markReviewCap` adds it on the
review poll to a PR that reached the cap earlier, also one from before this
change. A body with the marker is not written again.

## Why the reviewer trusts the marker

The reviewer applies the cap (`Assessment.AtCap`) only when all hold:

- the binding has `deferAtReviewCap`;
- the PR author is `jomcgi-factory[bot]`;
- one GraphQL read (`PullRequestBodyProof`) shows the body with the marker
  once, on the reviewed head, authored by the factory App and last edited by
  it or never edited.

Only the connector writes PR bodies with the factory App: a coder token has
`contents: write` alone (`broker/config`). A body edit by a person, a
workflow token or another App makes the reading `unverified`, and the normal
verdict applies. The check state records the reading (`State.ReviewCap`).

## What still blocks at the cap

`AtCap` defers an open medium finding only. These keep blocking: critical and
high findings, the human gate, Semgrep evidence, a previous finding the
report did not account for, and a finding whose decision Joe edited or
deleted. Incomplete review history keeps the check failed. Deferred findings
stay in the check state (`State.Capped`), so the next head must account for
them, and `State.Previous` lists them.

When the connector marks the PR after the reviewer failed the head, the
reviewer reviews that head once more per process (`capRecheck`).

## Rollout

`deferAtReviewCap` stays off in `deploy/values.yaml` until merge-time filing
of deferred findings is live
([#8446](https://github.com/jomcgi-org/homelab/pull/8446), HOME-640). Then one
values change turns it on for both services. A re-cut of a PR that still has
a critical or high finding at the cap is a separate change.
