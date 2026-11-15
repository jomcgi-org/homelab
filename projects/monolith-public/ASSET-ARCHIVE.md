# Public immutable asset archive

Cached HTML and open tabs can refer to an older frontend after a deployment.
Currently, `/_app/immutable/` reaches only the current pod's files. A missing
chunk can produce a 404 and sometimes a second document navigation through the
`_fresh` recovery handler. [#7998](https://github.com/jomcgi-org/homelab/issues/7998)
records the production failures.

## Publication and serving

The public chart's `publish-assets` PreSync Job runs in `monolith-workflows`.
Its init container uses the exact same digest-pinned public frontend image as
the Deployment. Node copies only `/app/build/client/_app/immutable` into a
bounded temporary volume. The copy rejects symlinks and non-files, and stops
before writing if the source exceeds 4,096 files, 16 MiB per file or 64 MiB in
total. The private/friends build, server output and mutable `version.json` are
not inputs.

The uploader uses the release's public backend image and its packaged Python
environment, but receives the existing `monolith-r2-s3` writer only inside
this Job. That Secret is already cloned into the jobs namespace by Kyverno.
The Job mounts no API token, adds no RBAC and receives no database or GitHub
credential. Both images are already registered in the public chart's Bazel
image map and must be published before chart promotion.

Publication uses the existing `artifacts` bucket and a dedicated
`public-frontend/immutable/` namespace, separate from untrusted artifact HTML.
The publisher audits the bucket lifecycle, checks every uploaded object's
bytes by reading it back, rejects changed identity content at an existing
hashed path, and records completion only after the entire release is available.
If a later compressor emits different gzip or Brotli bytes for identical
identity content, publication keeps the previous verified wire representations.
It verifies all of those existing objects before accepting the path, including
when a concurrent publisher wins the conditional manifest creation. Failure stops PreSync
and prevents the new HTML Deployment from starting. A retry is idempotent;
failed Jobs remain available for diagnosis until the next sync replaces them.

The backend exposes a bounded internal reader using its existing read-only R2
identity. No object-store credential enters the frontend or browser, and no
new public `/api` route is added.

This archive-only release keeps frontend serving, configuration and Deployment
ordering unchanged. It records this release’s public frontend image
before a later release enables archived-asset fallback. That activation must
install middleware before Kit's reserved `/_app` rejection and verify the
current entry asset through the configured read-only backend before listening.
It must also preserve the final cookie/auth/private-response cache guard.

## Retention and bootstrap

The archive is append-only. It does not delete objects, change an account
lifecycle, create a bucket or choose a retention TTL. Known matching expiration
or transition rules fail publication, as do malformed, unsupported or failed
configuration reads. The one exception is an explicit `AccessDenied` for the
configuration audit: publication records the lifecycle as unverified in its
completion metadata and logs a warning. This does not grant a retention-duration
guarantee or expand the existing object writer into an account administrator.
All object writes and readback checks still fail closed.

[#7960](https://github.com/jomcgi-org/homelab/issues/7960) still owns verification
of any account-managed lifecycle and the eventual freshness/deletion contract.
Current HTML policies include one-year
stale-if-error allowances; rollback and open tabs with deferred imports also
matter. A short guessed deletion window would recreate the original defect.

The first compatibility rollout does not repair all previously removed builds
or old pods without fallback support. Backfill older known public image digests
using the same validated publisher where those images remain available. Do not
substitute private images or assume the current build can recreate an old hash.
Only after every backend/frontend pod supports the archive can the next
release establish the protected A-to-B rollout guarantee. The existing reload
handler remains a last resort, not evidence that a first visit was fast.

The declared Argo CD controller has cluster-wide resource authority, and the
same default project already manages resources in `monolith-workflows` from
the monolith chart. No new controller permission is introduced. Effective
account lifecycle and ACL checks occur through the publication/read probes;
source declarations alone are not production verification.

## Validation

`//projects/monolith:public_assets_chart_test` renders both the base and GKE
charts and checks publication/Deployment ordering, exact image reuse, writer
namespace, token/RBAC absence, read-only public runtime identities and bounded
volumes. It executes the rendered Node copy against exact-byte, compressed,
worker, sibling-server, symlink, empty, conflicting and oversized fixtures.
Publisher/reader tests cover storage failures, equivalent recompression,
concurrent publication and path/content boundaries. The chart test also checks
that frontend fallback remains disabled in this archive-only release.

`//projects/monolith/public_assets_image:publisher_image_smoke_test` loads the exact built public
backend OCI image and verifies its config digest against the local engine. It
renders the chart's publisher command and runs that packaged interpreter with
`-m artifact.publish_public_assets --help`, then verifies the lazily imported
storage SDK and certificate bundle in the same image. The containers run with
no network, credentials, mounts, pulls or capabilities, a read-only root and
UID 65532. This required local CI test starts a bounded private Docker engine
when the runner has none; unavailable execution capability fails the test.
It never substitutes the test runner's Python or skips image execution.

After frontend fallback is enabled, live acceptance requires a healthy verified
rollout, a subsequent release with
alternating old/new pods, and requests for every retained initial/deferred
asset, including workers, with browser/CDN misses. Record exact bytes, MIME and
encoding, zero missing chunks and no `_fresh` navigation. Before/after mobile
timings remain separate from these functional assertions; local tests do not
establish a production speedup.
