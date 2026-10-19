# Fixture previews

## Compare before and after

The public entry page compares screenshots on one page. Native Before/After
radio controls preserve the screenshot frame's scroll position. Fixture, view,
phone/desktop width and light/dark selectors choose the same capture state on
both sides. Full baseline and head SHAs stay visible. Links open each revision's
interactive fixture or map gallery separately.

The build checks out the exact PR head and computes `git merge-base` against
the event's base SHA. It captures that real ancestor in a separate disposable
worktree with its own frozen dependency install. Neither revision receives a
publication credential. A pair requires matching synthetic inputs, fixture
harness files, capture code, Playwright and Chromium versions. Identical PNGs
are labeled identical. Changed harnesses or fixture data are labeled
unavailable for comparison; the baseline is never replaced with a different
revision. A missing baseline harness is also shown as unavailable. In
particular, a new Trips or Stars harness cannot supply historical screenshots
that were never captured. Their owner must establish a comparable baseline
before visual before/after acceptance can be claimed.

The comparison uses local PNGs under the existing CSP. It loads no production
API, external tile, worker or remote image. Browser checks decode every
referenced PNG, operate the selectors and keyboard radio controls, verify
preserved scroll offsets at phone/desktop widths in both browser schemes, and
check that an unavailable side displays no substitute image. Missing captures
from an available harness fail the build. Head and baseline evidence remain
separate under `head/` and `before/` in the exact-head evidence artifact.

Run the complete comparison from a clean committed checkout with the pinned
dependencies and browser installed:

```sh
python3 bazel/tools/previews/compare.py --expected-sha "$(git rev-parse HEAD)" \
  --base-sha "$(git rev-parse origin/main)" \
  --output /tmp/fixture-comparison --evidence /tmp/fixture-comparison-evidence
```

## Browse synthetic screens

`bazel/tools/previews/suite.py` builds one navigable artifact from the registered
harnesses present at the tested commit. The blog and factory demos remain
interactive. The private review demo composes the actual `ReviewCard`,
`ModeToggle` and `UndoToast` with invented records and local callbacks. It
does not import the private page, loaders, authentication or API clients.
Its build permits only those three private components. Other private screens
need their own reviewed synthetic boundary before registration.
The existing review controls retain their desktop layout in a named, focusable
scroll frame on phones. The receipt records that frame's visible and content
widths separately from page overflow. This composition does not establish that
the production private review page is responsive.

The suite runs each existing contract and browser checker. It adds 16 private
review captures, measured document/body overflow and keyboard decision and
delete/undo checks. It verifies 9 blog captures and 40 factory review captures
in addition to the factory checker's complete interaction matrix. Missing,
empty or invalid PNGs fail the build. Each suite records the source SHA,
Playwright and Chromium versions, output digests and required PNG digests in
`suite.json`. Assertions retain failure PNGs and traces. The assembled artifact
is checked again at 390px and 1440px under a nested path and validated with the
publisher's existing archive, size and CSP checks. Tests cannot rewrite the
published bytes.

Trips and Stars remain owned by [#7213](https://github.com/jomcgi-org/homelab/pull/7213)
and [#7217](https://github.com/jomcgi-org/homelab/pull/7217). When their harnesses
are present, the suite calls their supplied `check` functions. Trips requires
all 16 base page captures plus 8 viewer/map captures; Stars requires all 8
live/historical captures. Shared instrumentation records browser versions and
traces, supplies an invented blank Stars basemap, and denies unknown remote
requests. Trips keeps its owner's synthetic style and terrain responses.
These checks exercise real WebGL controls with invented data and no geographic
tile acceptance. The public map demos are navigable PNG galleries, containing
no map JavaScript or workers. `connect-src 'none'` and `worker-src 'none'` stay
unchanged. The original interactive map artifacts remain evidence inputs.

After installing the frozen workspace dependencies and Python Playwright
1.63.0 with its Chromium, commit the source and run from the repository root:

```sh
python3 -m unittest discover -s bazel/tools/previews -p '*_test.py'
python3 bazel/tools/previews/suite.py --expected-sha "$(git rev-parse HEAD)" \
  --output /tmp/fixture-demo --evidence /tmp/fixture-evidence
```

Both output paths must be fresh. `--suite review` selects one registered suite
for focused testing. `--source /path/to/owner-worktree --suite trips` runs the
same tooling against an unchanged owner checkout, recording both source and
runner commits. It neither rebases the owner branch nor restarts a factory job.
Use a credential-free local process for PR-owned code. The Actions build
already has read-only access, no persisted checkout credential and no secrets.

The build workflow uploads `fixture-preview-<full SHA>` for publication and
`fixture-preview-evidence-<full SHA>` separately for screenshot review. Evidence
requests 90-day retention, bounded by the repository's Actions retention policy;
the publish input retains 14 days. Evidence includes PNGs, logs, JSON receipts
and traces, and is never copied into the public aggregate. Download and retain
the exact-SHA evidence before its Actions expiry if it is needed longer.
Replacing a served SHA or closing a PR does not remove that separate artifact.

Private previews never contain production records, credentials or authenticated
connections. Adding a preview does not authorize changes to production access,
Pages ownership, deployment environments or CSP.

## Existing blog contract

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
On 2026-10-19 the current integration received 404 from the Pages settings
endpoint, 403 from repository variables, and 404 for `fixture-preview-state`.
The five latest publisher runs were skipped. Those reads do not establish
Pages ownership or prove that no site exists.
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
variable after that run. Activation requires separate approval of these exact
settings. The reusable-suite change leaves all activation controls disabled.

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

The dedicated state branch stores the desired aggregate of last validated passing
builds. State advances before deployment; if deployment fails, a later
reconciliation can publish those validated bytes. It is not a transactional record
of what is currently live. Cleanup removes served files and current branch-tree
entries, while parent-linked Git history retains historical public fixture bytes.
The branch is not a secret store and its history is not purged by the TTL.

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
