# DR jobs fixture preview

This preview imports the production `/app/dr-jobs` page. Its listings are
invented and live only here. The browser clock is fixed at October 19, 2026,
12:00 UTC. The local font and static artifact server come from `preview/`.

From `projects/monolith/frontend`, after installing frozen workspace dependencies:

```sh
pnpm exec vitest run src/routes/public/app/dr-jobs/page.dom.test.js src/routes/public/app/dr-jobs/page.server.test.js src/lib/public/dr-jobs/urlParams.test.js
pnpm exec vite build --config dr-jobs-preview/vite.config.js
python dr-jobs-preview/check_browser.py --artifact dr-jobs-fixture-preview --output /absolute/path/to/evidence
```

The browser runner needs Playwright 1.63.0 and Chromium. `--executable` can
select a local Chromium binary. CI uses the pinned Playwright browser.

`check_browser.py` saves live, history, empty-live and empty-history captures
at 390 and 1440 pixels in both schemes, plus focus captures, traces and
`checks.json`. It checks exact page overflow, native keyboard controls,
computed text contrast, URL initialization/writeback, unrelated parameters,
town reset, scheme persistence and the 30-minute refresh with teardown.
The DOM suite also hydrates actual server-rendered markup without replacing
job links, and composes the production layouts to check navigation suppression.

The fixture navigation adapter records `goto` and `invalidateAll` calls. It
does not emulate SvelteKit server requests or prove real-host routing/cache
behavior. The unchanged server-load tests cover the SSR data contract.

The factory fixture build also builds this page in a separate module graph
under `factory-fixture-preview/dr-jobs/`. Its existing browser runner captures
this page before the factory matrix. The unchanged `Factory mobile preview`
workflow uploads these pre-merge captures in the `dr-jobs/` directory of
`factory-preview-evidence-<head SHA>`. Keep generated output outside Git. Live
rollout and real-host acceptance remain on #7147 after a separately authorized
merge and publication. The delivery PR remains draft under its landing hold.
