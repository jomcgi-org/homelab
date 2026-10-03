# Fixture previews

The first preview renders the public 4090 blog page using the production page,
layout, replay controls, graph and styles from the PR commit. Its article,
inference events, timings and graph content are invented. A visible banner
identifies the fixture. It has no API proxy, credentials, telemetry or runtime
backend. The production SvelteKit adapter stays unchanged.

The harness lives in `projects/monolith/frontend/preview/`. The separate build
configuration uses relative asset URLs, disables the public asset directory and
source maps, replaces the captured replay with synthetic JSON, and rejects
server/private modules and production recording manifests in the module graph.
The conformance replay is outside this first slice and has an explicit placeholder.
The production Schibsted Grotesk face is vendored unchanged from Google Fonts
blob `78ad61cf65a390c517248f46d325a1e9b88d367d`, with its SIL Open Font License
included in the output. Browser checks wait for that local font before capture;
there are no remote font requests. Page metadata omits the production canonical URL. Breadcrumb navigation is bounded
to the fixture; article fragment links and playback remain interactive.

## Test the same artifact

Existing BuildBuddy CI picks up the Bazel `fixture_preview_build_test` and
`fixture_contract_test` targets under `//projects/monolith/frontend`. The small
GitHub Actions workflow uses the same Vite configuration and frozen pnpm lock to
build once, then tests that directory in Chromium and uploads it unchanged.
It does not restore a BuildBuddy runner or use its credentials. BuildBuddy's
current `pr-checks` runs PR-owned YAML with injected credentials, so it is not
the publication trust boundary for these previews.

From the repository root, after the normal frozen dependency installation:

```sh
pnpm --dir projects/monolith/frontend exec vitest run --config preview/vitest.config.js
pnpm --dir projects/monolith/frontend exec vite build --config preview/vite.config.js
python3 projects/monolith/frontend/preview/browser_check.py \
  --artifact projects/monolith/frontend/fixture-preview \
  --output /tmp/fixture-preview-evidence
```

The browser check needs Python Playwright 1.63.0 and its Chromium installation,
as does the existing Grimoire browser runner. It serves the output at a nested
Pages-style prefix and refuses every browser request outside that prefix.
Viewports are 360x640, 390x844 and 1440x1000. Checks cover opening layout,
horizontal overflow, play/pause/replay, the completed graph, article access,
theme persistence and breadcrumb confinement. Opening, full-page and completed
screenshots plus traces are uploaded even when an assertion fails. File digests
before and after the check ensure tests did not rewrite the publish directory.
The successful static artifact and browser evidence are separate uploads.

`preview/fixtures/blog-page.json` is shared by the browser harness and hermetic
contract tests. The tests import the actual `+page.server.js` from this PR,
replace only its post manifest with `preview/fixtures/posts.json`, and compare
its complete result with the typed fixture. A changed field, type or rendered
body fails the test. Update the fixture deliberately after reviewing the loader
change. `preview/blog-contract.js` documents and checks this narrow shape.

The blog has a local SvelteKit server-load contract, not a Grimoire HTTP API.
No claim is made that this checks all backend contracts. A future API-backed
scenario needs its same-commit response model/serializer tests and shared
synthetic response fixtures before it joins this preview. Broad `dict[Any]`
responses need narrower types at that boundary; this change does not overhaul
Grimoire models.

## Enable publication separately

This PR adds implementation only. No Pages site, branch protection, environment,
credential, OAuth grant, token or repository variable is created by the author.
The publisher is disabled unless `FIXTURE_PREVIEWS_ENABLED` is `true`.

A repository administrator must first inspect the existing Pages configuration
and confirm this repository's Pages site is dedicated to public fixture previews.
The connected integration used to prepare this PR could not read the Pages
settings endpoint. There were no existing GitHub Actions workflows in the
inspected repository. Those facts do not prove there is no existing site.
Do not replace an unrelated Pages deployment.

After authorization, use GitHub Pages' Actions publishing source, retain the
`github.io` hostname with no custom domain, configure the `github-pages`
environment to admit only the default branch, and review the publisher's bounded
job permissions. The publisher needs repository contents write for its dedicated
aggregate state branch, Pages write and OIDC for deployment, Actions read to
retrieve artifacts, and pull-request write for its one status comment. No PAT or
cloud credential is required. Bootstrapping the dedicated state branch and
setting the enable/ownership variables are explicit activation steps. Set
`FIXTURE_PREVIEWS_OWN_PAGES_SITE=true` only after confirming the entire existing
site may be managed by this publisher, then set `FIXTURE_PREVIEWS_ENABLED=true`.
The first run creates `fixture-preview-state`; remove the ownership/bootstrap
variable after that run. Do not activate this draft.

Only after merge and activation can a tested PR receive a working link. The
publisher uses the Pages API URL returned for the real site. There is no preview
URL to visit before the first successful deployment.

## Publication boundary

The build runs PR code with read-only repository access on a fresh hosted
runner. Its checkout does not persist credentials. It receives no publish or
production secrets. The privileged publisher runs only default-branch-owned
code, downloads the successful run's static artifact as data, validates its
repository/workflow/PR/head association through GitHub, and rejects stale or
closed PRs. Artifact metadata cannot choose a command, checkout, branch or PR.

The aggregate site preserves active previews at `pr/<number>/<sha>/` with a
stable `pr/<number>/` link. One serialized workflow handles both publication and
cleanup. It validates file types, size/count limits, paths and the restrictive
CSP without executing artifact code. The HTML, CSS and JavaScript tested in the
build remain byte-identical when copied into the aggregate. Trusted index and
redirect pages live outside that artifact.

Closed PRs are removed on the close event; daily reconciliation supplies a TTL
backstop. Reconciliation and deployment recheck a new candidate's head/state so a completed old
build cannot move a PR's stable link backwards. Existing links retain the latest
passing build while a new commit is pending. Each replacement retires that PR's
previous commit path; other open PR previews are preserved. GitHub head/state
can change while a Pages deployment is in flight, so post-deploy checks and
subsequent cleanup remain necessary. Comments identify the exact tested commit.

Every preview on a repository Pages site shares one public browser origin.
The CSP and browser tests restrict resource loading, and no credentials or
private data are present. This is not isolation for hostile JavaScript or a
place to authenticate to production. CSP cannot reliably forbid every top-level
navigation. Do not add production login, API credentials, service workers,
private fixtures or privileged browser bridges. The Friends, Authentik and GCS
designs are deferred and are not implemented here.

## References

- [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [GitHub workflow events and workflow_run security](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GitHub Actions secure use](https://docs.github.com/en/actions/reference/security/secure-use)
