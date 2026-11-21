# Factory Git push receiver

Install `pre-receive` in the hooks directory of a private bare staging
repository seeded from the public repository. Run the receiver with Git and
Python 3. Set `PUSH_UPSTREAM` to the trusted HTTPS URL. If using a
named remote, configure only its URL. A fetch refspec makes Git try to update
remote-tracking refs while the hook is still in receive-pack quarantine. Scope receiver access to the corresponding
coder binding and work branch. The receiver needs the binding's write
credential. Keep credential values out of remote URLs.

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

The factory chart routes the public homelab coding binding to
`https://factory-push-guard.factory-r-homelab.svc.cluster.local/jomcgi-org/homelab.git`
when `repositories[].guardedPush` is true or its private harness image is
selected. Provision that TLS Git smart-HTTP endpoint and its gateway network
policy before setting the flag. Terminate authenticated requests only for
this repository, validate the injected binding credential and keep it on the
trusted side. Use a synthetic index on a test receiver for live GitHub
acceptance, then install the production private index before supplying
Mandoline to coders.
