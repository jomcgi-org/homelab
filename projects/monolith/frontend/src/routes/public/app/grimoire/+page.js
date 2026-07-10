// The landing page is a static pitch: no /api/grimoire fetches, no corpus
// content beyond the one Joe-approved excerpt baked into ScrollStory's static
// assets. It carries none of the risk that turned SSR off for the rest of the
// route tree (see the sibling +layout.js), so this page-level option
// overrides the layout's ssr=false and lets crawlers, curl, and link
// unfurls see the real pitch instead of an empty shell.
export const ssr = true;
