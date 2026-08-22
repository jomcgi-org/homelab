# Qwen lane probe

This CLI measures a model-bench task through the real monolith agent-session API on
the in-cluster qwen and pi lane. It records end-to-end wall time, the timing carried
by the completed turn, the resulting repository diff, verifier correctness, and a
best-effort SigNoz span breakdown. It does not modify the lane.

From `projects/model-bench`, first expose the monolith backend:

```sh
kubectl -n monolith port-forward svc/monolith 18000:8000
```

The orchestrator publishes each task snapshot as `bench/<task-id>` in
`jomcgi/homelab`. Fetch those branches and snapshot commits before running the probe.
The probe only reads the remote branch through the guest clone and the local checkout
through `git archive`.

Run the fast or long set twice and write JSONL results:

```sh
python3 -m probe sets
python3 -m probe run --set fast --reps 2 --out results.jsonl
python3 -m probe run --set long --reps 2 --out results.jsonl
python3 -m probe report --in results.jsonl
```

Add `--reasoning` to keep qwen thinking on for every invoke in each session.

The fast set is `commit-message-01`, `slo-budget-breach-01`, and
`null-content-fix-01`. The long set is `go-vsock-frame-01`,
`worldcup-swing-settled-01`, and `research-adr-writeback-01`.

The hop table groups spans into the Cloudflare gateway, Context Forge, monolith,
EmberVM control, and guest or shim services. `total_ms` is the sum of span durations,
so overlapping spans can make it larger than wall time. `max_ms` highlights the
largest observed span. Empty buckets remain visible, along with the distinct service
names that were observed. Span collection is best effort and `--no-spans` skips it.

For the efficiency loop in issue #5051, use two repetitions and compare medians. Keep
a change only when correctness still holds and median wall time improves by at least
5 percent, or when completion on the long set improves.
