# Restore-time vsock override acceptance gate

This is the manual readiness gate for changing EmberVM's restore-time vsock
path. Passing unit tests or a successful Firecracker snapshot-load response is
not sufficient. The candidate must run on KVM and exchange bytes with real
guests before its pull request leaves draft.

The in-cluster conformance runner's S1 scenario is the executable data-path
assertion. It submits two sandbox tasks concurrently from one ready base. Each
request carries a different token from host to guest, each guest returns its own
token to the host, and either response containing the peer token is a failure.
The hold and startup headroom come from the chart's `cloneHold` and
`cloneStartupHeadroom` values. The overlap budget is their sum. The headroom
must be shorter than the hold so the budget allows restore, admission, and HTTP
latency while still rejecting two serialized holds.

## Evidence contract

Keep one immutable log or issue attachment for every matrix row. Each log must
include:

- candidate commit SHA and chart or image identity;
- `uname -m`, Firecracker version, and jailer version;
- the rendered `EMBERVM_NODED_JAILER_ENABLED` value;
- whether the start was cold boot, clean-base restore, or crash relaunch;
- the S1 `/verdict` JSON, including both clone markers, overlap duration, and
  final VM reap;
- the noded log lines for both VM IDs, their distinct host `v.sock` paths, and
  the applied device paths (the jailer path is `/vsock.sock`);
- for crash relaunch, `ls -l` output showing the leftover `v.sock` and at
  least one `<uds>_<port>` child before the relaunch, followed by the successful
  relaunch and cleanup result.

Post links to the logs on the tracking issue and copy those links into the pull
request body. Do not mark the pull request ready from a verbal report or from a
mutable dashboard view.

## Required x86_64 matrix

Run only in an isolated test namespace or disposable node pool. Never use the
production namespace for crash injection.

| Launch mode | Cold boot | Two-clone restore | Crash relaunch over stale sockets |
| --- | --- | --- | --- |
| direct exec (`EMBERVM_NODED_JAILER_ENABLED=false`) | required | required, S1 must pass | required |
| jailer (`EMBERVM_NODED_JAILER_ENABLED=true`) | required | required, S1 must pass | required |

For each launch mode:

1. Deploy the exact candidate to the isolated environment and capture the
   rendered launch-mode value and binary versions.
2. Use a new test workload or image identity so no base exists. Capture the
   cold boot reaching guest readiness and completing one bidirectional task.
3. Wait for that workload's clean base to report ready. Run S1 and retain its
   complete verdict plus the two VM-specific socket paths. A pass proves the
   two requests overlapped, each guest returned only its own token, and both
   VMs were reaped.
4. Start another S1 cycle. During the guest hold, kill one candidate
   Firecracker process with `SIGKILL`, not noded. Before retrying, capture the
   leftover base socket and a port-suffixed socket under that VM's bundle.
5. Invoke the same workload again. The relaunch must become ready, exchange the
   correct token in both directions, and leave neither stale path in place. A
   launch that succeeds only after an operator removes a socket fails this row.
6. Repeat steps 1 through 5 with the other launch mode. Jailer evidence must
   also show that the host alias resolves beneath the jail root and that the
   jail uid and gid can traverse the staged directory chain.

## aarch64 status and waiver

The same matrix should run on a KVM-capable aarch64 node. If no such node is
available, the pull request may remain technically reviewable only when its body
states that aarch64 runtime validation is incomplete, identifies the missing
KVM resource, and links the following support evidence:

- the repository's pinned aarch64 Firecracker and jailer versions and digests;
- the matching v1.16.1 API specification that defines
  `SnapshotLoadParams.vsock_override` without an architecture restriction;
- the architecture-neutral EmberVM request serialization and path-selection
  tests.

That waiver does not replace the required x86_64 matrix. It records why the
unvalidated architecture is accepted for review and makes the missing runtime
coverage explicit. A later aarch64 run should append its immutable logs to the
same tracking issue.

## Stop conditions

Keep the pull request draft if any required x86_64 row is missing, S1 is absent
from the candidate image, the verdict is from a different commit, either marker
crosses to the peer response, the two guest holds serialize, or stale sockets
need manual cleanup. Record aarch64 as either passed or explicitly waived, never
silently untested.
