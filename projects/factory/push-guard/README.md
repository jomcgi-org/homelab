# Factory Git push receiver

The receiver image seeds a private bare repository from public homelab.
It serves Git push discovery and receive-pack for that repository. The
Substrate gateway authenticates the binding and presents its pod identity
certificate. The receiver sends the injected binding credential only to
GitHub. The pre-receive hook permits only factory work branches.

Generate the index privately from the pinned Bazel outputs for
`//bazel/tools/mandoline:mandoline` and `//bazel/tools/mandoline:layer`.
Run `pre-receive --index BINARY LAYER > index.json` under umask 077 and set
`MANDOLINE_INDEX` to the resulting private file. Rebuild it on every pin
change. It contains short lookup prefixes. Keep the inputs and index
outside guest-writable storage and public repositories.

The receiver checks every newly reachable blob, then pushes the accepted
object IDs atomically with leases against the GitHub refs it just read. If a
branch advanced since staging advertised it, the accepted tip must include
the current GitHub tip. A concurrent update after that read fails the lease.
A receiver-owned `PUSH_SIZE_ALLOWLIST` can name a file of SHA-256 digests
exempt from the 1 MiB size limit. Content checks still apply. The scanner
samples 192 bytes every 16 KiB and recognizes raw bytes, hex, and standard
or URL-safe base64 across split alignments and line wraps. Other transforms
and fragments between samples can escape recognition.

Before enabling the private coder image, route coder writes exclusively
through the trusted receiver and remove direct GitHub write authentication
from the sandbox egress policy. Verify rejection and acceptance from that
binding using a synthetic private reference and harmless push fixtures.

After CI publishes the image and chart, set `pushGuard.enabled`,
`pushGuard.image.digest` and `pushGuard.indexSHA256` in reviewed values.
The seed container waits for the private index. From an authorized operator
session, copy it to a temporary file under
`/var/lib/factory-push-guard/indices/` on the receiver's volume. Rename it to
`<sha256>.json` after the copy completes. The receiver verifies that digest
before it listens.

Wait for receiver readiness before setting `repositories[].guardedPush`.
Use a synthetic index and harmless fixtures to verify normal pushes and
rejections from the active coder binding. Switch the index pin to the
production index and verify the new receiver is Ready before selecting the
private coder image.
