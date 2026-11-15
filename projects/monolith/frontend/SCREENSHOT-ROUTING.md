# Public screenshot routing

The screenshot tool navigates Chromium to
`http://monolith-public-frontend.monolith-public.svc.cluster.local:3000`.
The destination is the public frontend, but the browser uses that internal
hostname instead of `jomcgi.dev`. The guest rewrites the whole origin because
Chromium's `.dev` HSTS policy would otherwise upgrade the plaintext internal
hop to HTTPS.

The universal `src/hooks.js` reroute hook now recognizes that exact hostname
as a public origin. Server rendering and subsequent browser links and proxy
requests resolve to `/public/*`, just as on the apex. Private and friends
paths remain under `/public/private/*` and `/public/friends/*`, where they do
not resolve to the private or friends route trees. Other Service names and
lookalike hostnames are not aliases.

The guest mapping is in
`projects/embervm/runtimes/shotter/etc/shotter-egress.json`; navigation rewriting
is in `guest-init/cmd/screenshot.go`. The proxy forwards the internal Host
unchanged. This change does not alter that mapping, its allowlist, runtime
identities, or the screenshot tool's metadata contract.

The `svc-url` source-ratchet exception covers only this inbound Host comparison.
The hook never opens a connection to the literal. Shotter already bakes that
exact identity into its guest mapping, so a Service rename must update both
the mapping and this alias together. Keeping the accepted alias fixed also
keeps browser and server routing identical without another runtime hostname
setting. The exception does not apply to outbound Service URLs.

## Evidence and verification

On 2026-11-15, the tool returned 404 images for public Hikes and Stars while a
direct public Hikes request returned HTTP 200. A local packaged public build
reproduced the routing difference without backend data:

| Host                    | Path                | Before |
| ----------------------- | ------------------- | -----: |
| `jomcgi.dev`            | `/app/frank`        |    200 |
| Public Service hostname | `/app/frank`        |    404 |
| Public Service hostname | `/public/app/frank` |    200 |

The regression uses the packaged HTTP entry to require identical Frank HTML
on the apex and public Service origin. It also covers cookies and identity
headers remaining `no-store`, private/friends path isolation, and unrelated
hosts. Universal-hook tests cover the app routes and lookalike Service names;
a browser-environment test resolves normal links and proxy paths from the
actual internal origin. These files are included by the frontend `:test_lib`
glob; the HTTP test consumes the existing `:build` dependency.

The public-only build must also pass the packaged HTTP tests. After deployment,
repeat the tool capture for Hikes and Stars and verify real snapshot content.
These checks establish routing correctness; the in-cluster screenshot tool
bypasses Cloudflare and does not establish CDN or mobile performance.
