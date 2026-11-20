# Factory Explorer

See the [explorer architecture](../factory-explore/ARCHITECTURE.md).

The read-only Sparkles fork in `projects/factory-explore/ui/` is based on xdavidwu/sparkles commit
`fe113cfbe1f5db41a8e25940adb6d28195a23686` (MIT). Its Vue application
uses Authentik's factory-explore public client. The HOME-355 blueprint registers the redirect URI
`https://factory-explore.jomcgi.dev/oidc/callback` on that client.

Build with `bazel build //projects/factory-explore/ui:build`. The chart's
sync Job uploads the static files to the factory-explore Cloudflare Worker
using a dedicated factory-explore-cloudflare 1Password item. The browser
queries the explorer API directly with the signed-in user's ID token.

The platform kustomization registers the publisher in its own namespace,
`factory-explore-site`. Its image and chart use that same suffix so they
can coexist with the HOME-356 API proxy in `factory-explore`.

Publishing is staged ([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-360#comment-3945)):
this change keeps `publishEnabled: false`, and HOME-388 turns it on.
Sequence the first publication after HOME-358 exposes the HOME-356 proxy
at factory-explore-api.jomcgi.dev. The Application stages hostname management with publication disabled.
Before enabling publication, verify CORS from the UI origin, an authenticated read, and
server-side denial of anonymous reads and writes. Record Joe’s decision on serving the SPA before sign-in, or verify that the
hostname requires sign-in at the edge. Provision the dedicated credential,
then set the Application's Helm value `publishEnabled` to `true` through Git.
After publication, verify
sign-in and a resource listing from a fresh browser.

Keep `manageHostname: true` on the Application. Once the chart and publisher
image containing the withdrawal command are pinned, reverting the publication
override runs a Sync Job that detaches the explorer hostname. Verify from a
fresh browser that the hostname no longer serves the explorer. If rollback
must use an older chart, detach the custom domain in Cloudflare before
lowering the pin.
