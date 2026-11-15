# Trip gallery loading

The timeline for `2025-liard-hot-springs` contains 4,838 photos. In the hosted
mobile baseline, native lazy loading started 44 image requests between
1,113 and 1,126 ms after navigation. The third gallery image became LCP at
4,116 ms in that run, after its request completed at 4,098 ms. Median LCP
across the three runs was also 4,116 ms. This baseline used the public
performance harness with 4G throttling; it does not describe the changed code.

`PhotoGrid` now includes six eager, high-priority images in its initial HTML.
Six cover the widest gallery row at the default sheet width and three rows
at the measured mobile width. A shared IntersectionObserver adds later image
URLs within 160 pixels of the viewport. Keyboard focus also loads a photo.
The square tiles reserve their existing space before images load.

Every photo keeps its original URL, order, button and lightbox index. Tag
filters retain the same collection behavior. The HTML includes later images
inside `noscript`, so scrolling with JavaScript disabled still shows the full
gallery. Svelte omits those images from the client render. Browsers without
IntersectionObserver fall back to native lazy loading for later photos.

## Local measurements

Measured on 2026-11-15 against
`f2a480147f9e04609b4eacda4454fbe9d3507f0a`, using Node 24.22.1 and Svelte 5.56.9.
Each component was rendered once to warm up, then three times with the same
saved public trip point list. The table records median component SSR time and
the last rendered HTML. Gzip uses Node's default `gzipSync` settings.

| Measure | Before | Changed |
| --- | ---: | ---: |
| Complete photo collection and buttons | 4,838 | 4,838 |
| Initial image URLs outside `noscript` | 4,838 | 6 |
| Image URLs with JavaScript disabled | 4,838 | 4,838 |
| Eager, high-priority images | 0 | 6 |
| Component SSR median | 14.04 ms | 22.33 ms |
| Component HTML bytes | 1,255,747 | 1,444,429 |
| Component gzip bytes | 252,132 | 253,463 |

The complete no-JavaScript fallback adds 1,331 gzip bytes and increases SSR
work. The snapshot SHA-256 is
`2b0bbafb885b6a948a98fbd45e8c1cfc9f5f2f85951792e1a5747594e7e5a8c9`.
In a separate three-run HappyDOM check of the same collection, component
mounting took a median 298.64 ms before and 311.20 ms after. Revealing the next
six images took 1.65 ms after the change. Readiness is tracked per image URL;
replacing a shared Set invalidated all 4,838 tile conditions and took 27.35 ms
for the same reveal in an intermediate implementation. These DOM timings use
a mocked observer, exclude layout and image requests, and are not mobile INP.

These counts measure available image URLs, not browser requests. A large
viewport can immediately intersect more than six photos. Requests already
started remain subject to the browser's scheduling. Post-deploy mobile LCP,
request counts and transfer remain acceptance measurements.

The DOM regression uses 44 photos. It starts with six actual image elements,
keeps offscreen entries unloaded, and adds exactly two URLs after two tiles
intersect. It also covers keyboard access to the last photo, every lightbox
index, filtering, changed URLs on reused tiles, missing observer support and
unmount. Revealing a nearby image does not reread offscreen URLs; that
regression fails with shared-Set invalidation. The SSR regression checks every
URL and the no-fetch first view.
Both first-view assertions fail against the original component's 44 images.

```sh
cd projects/monolith/frontend
node node_modules/vitest/vitest.mjs run \
  src/lib/public/components/trips/photo-grid.dom.test.js \
  src/lib/public/components/trips/photo-grid.ssr.test.js \
  src/routes/public/app/trips
```

The frontend `:test` target includes both new tests through its existing glob.
