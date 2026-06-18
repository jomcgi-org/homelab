// Content-Security-Policy for the SvelteKit app (ADR 005 layer 8, Phase 4c).
// Consumed by svelte.config.js `kit.csp` (mode "auto" => nonce under SSR), so
// SvelteKit adds a per-request nonce to its own inline bootstrap scripts and
// this object only has to declare the host allow-list.
//
// Security intent (the point of this whole change): script-src carries NO
// 'unsafe-inline'. Inline event handlers (onerror=, onclick=) and any injected
// <script> therefore cannot execute, so untrusted public-chat model output and
// note bodies rendered into the DOM cannot run script even if the markdown
// renderer ever regressed. The markdown renderer (components/notes/markdown.js)
// is the first line: it HTML-escapes &<> and emits no raw HTML, links, or
// javascript:/data: URLs. The CSP is the second, independent line.
//
// Styles are deliberately permissive (relaxing styles, not scripts, is the
// tradeoff). 'style-src' is pinned to exactly ['unsafe-inline'] on purpose:
// SvelteKit only adds a style nonce when style-src (or its default-src
// fallback) contains a value other than 'unsafe-inline'. Keeping it to just
// 'unsafe-inline' suppresses that nonce, which matters because under CSP3 a
// nonce makes the browser IGNORE 'unsafe-inline', and that would break every
// Svelte `style:` directive (the maps and knowledge graph use them heavily) and
// any stylesheet a map library injects at runtime. 'style-src-elem' re-allows
// same-origin and Google Fonts stylesheets; 'style-src-attr' re-allows the
// inline style="" attributes those directives compile to.
//
// External origins (audited 2026-06-18 across public AND private routes; the
// other external hosts in the source, github/linkedin/jomcgi/etc, are <a href>
// navigation targets, not resource loads, so they need no directive):
//   - https://challenges.cloudflare.com  Turnstile api.js script + challenge iframe
//   - https://fonts.googleapis.com        Google Fonts stylesheet (public +layout head)
//   - https://fonts.gstatic.com           Google Fonts font files
//   - https://tiles.openfreemap.org       MapLibre basemap style/tiles/glyphs/sprite
//                                          (ships, stars, hikes maps)
// 'self' covers the same-origin OTEL passthrough (POST /otel/v1/traces) and all
// /api calls; blob: covers MapLibre web workers and canvas-derived image
// sources; data: covers inlined fonts/images.
//
// Note: <script type="application/ld+json"> (the SEO block in public/+layout)
// is a non-executable data block exempt from script-src, so it needs no hash.
export const cspDirectives = {
  "default-src": ["self"],
  "script-src": ["self", "https://challenges.cloudflare.com"],
  "style-src": ["unsafe-inline"],
  "style-src-elem": ["self", "unsafe-inline", "https://fonts.googleapis.com"],
  "style-src-attr": ["unsafe-inline"],
  "font-src": ["self", "https://fonts.gstatic.com", "data:"],
  "img-src": ["self", "data:", "blob:", "https://tiles.openfreemap.org"],
  "connect-src": ["self", "https://tiles.openfreemap.org"],
  "worker-src": ["self", "blob:"],
  "frame-src": ["https://challenges.cloudflare.com"],
  "object-src": ["none"],
  "base-uri": ["self"],
};
